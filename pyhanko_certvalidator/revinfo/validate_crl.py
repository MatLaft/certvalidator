import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Union, List, Optional, Dict, Tuple, Set

from asn1crypto import x509, crl, cms
from cryptography.exceptions import InvalidSignature

from pyhanko_certvalidator.context import ValidationContext
from pyhanko_certvalidator._state import ValProcState
from pyhanko_certvalidator.errors import PathValidationError, RevokedError, \
    CRLValidationError, CRLNoMatchesError, CertificateFetchError, \
    CRLValidationIndeterminateError, PSSParameterMismatch
from pyhanko_certvalidator.path import ValidationPath
from pyhanko_certvalidator.policy_decl import CertRevTrustPolicy
from pyhanko_certvalidator.registry import CertificateRegistry
from pyhanko_certvalidator.revinfo.archival import CRLWithPOE, \
    RevinfoUsabilityRating, ValidationTimingInfo
from pyhanko_certvalidator.revinfo.constants import VALID_REVOCATION_REASONS, \
    KNOWN_CRL_EXTENSIONS, KNOWN_CRL_ENTRY_EXTENSIONS
from pyhanko_certvalidator.revinfo.manager import RevinfoManager
from pyhanko_certvalidator.authority import Authority, AuthorityWithCert
from pyhanko_certvalidator.util import get_ac_extension_value, \
    validate_sig, pretty_message, ConsList, get_issuer_dn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CRLWithPaths:
    """
    A CRL with a number of candidate paths
    """

    crl: CRLWithPOE
    paths: List[ValidationPath]


async def _find_candidate_crl_issuer_certs(
        crl_authority_name: x509.Name, certificate_list: crl.CertificateList,
        *,
        cert_issuer_auth: Authority,
        cert_registry: CertificateRegistry) -> List[x509.Certificate]:
    # first, look for certs issued to the issuer named as the entity
    # that signed the CRL.
    # In both cases, we prioritise the next-level issuer in the main path
    # if it matches the criteria.
    delegated_issuer = certificate_list.issuer
    cert_issuer_cert = None
    if isinstance(cert_issuer_auth, AuthorityWithCert):
        cert_issuer_cert = cert_issuer_auth.certificate
    candidates = cert_registry.retrieve_by_name(
        delegated_issuer, cert_issuer_cert
    )
    if not candidates and crl_authority_name != certificate_list.issuer:
        # next, look in the cache for certs issued to the entity named
        # in the issuing distribution point (i.e. the issuing authority)
        candidates = cert_registry\
            .retrieve_by_name(crl_authority_name, cert_issuer_cert)
    if not candidates and cert_registry.fetcher is not None:
        candidates = []
        valid_names = {crl_authority_name, delegated_issuer}
        # Try to download certificates from URLs in the AIA extension,
        # if there is one
        async for cert in \
                cert_registry.fetcher.fetch_crl_issuers(certificate_list):
            # filter by name
            if cert.subject in valid_names:
                candidates.insert(0, cert)
    return candidates


@dataclass
class _CRLIssuerSearchErrs:
    candidate_issuers: int
    candidates_skipped: int = 0
    signatures_failed: int = 0
    unauthorized_certs: int = 0
    path_building_failures: int = 0
    explicit_errors: List[CRLValidationError] = field(default_factory=list)

    def get_exc(self):
        plural = self.candidate_issuers > 1
        if not self.candidate_issuers \
                or self.candidates_skipped == self.candidate_issuers:
            return CRLNoMatchesError()
        elif self.signatures_failed == self.candidate_issuers:
            return CRLValidationError('CRL signature could not be verified')
        elif self.unauthorized_certs == self.candidate_issuers:
            return CRLValidationError(
                'The CRL issuers that were identified are not authorized '
                'to sign CRLs'
                if plural else
                'The CRL issuer that was identified is '
                'not authorized to sign CRLs'
            )
        elif self.path_building_failures == self.candidate_issuers:
            return CRLValidationError(
                'The chain of trust for the CRL issuers that were identified '
                'could not be determined'
                if plural else
                'The chain of trust for the CRL issuer that was identified '
                'could not be determined'
            )
        elif self.explicit_errors and len(self.explicit_errors) == 1:
            # if there's only one error, throw it
            return self.explicit_errors[0]
        else:
            msg = 'Unable to determine CRL trust status. '
            msg += '; '.join(str(e) for e in self.explicit_errors)
            return CRLValidationError(msg)


async def _validate_crl_issuer_path(
        *, candidate_crl_issuer_path: ValidationPath,
        validation_context: ValidationContext,
        issuing_authority_identical: bool,
        proc_state: ValProcState):
    # If we have a validation cached (from before, or because the CRL issuer
    #  appears further up in the path) use it.
    # This is not just for efficiency, it also makes for clearer errors when
    #  validation fails due to revocation info issues further up in the path
    if validation_context.check_validation(candidate_crl_issuer_path.last):
        return
    try:
        temp_override = proc_state.ee_name_override
        if not issuing_authority_identical:
            temp_override = (
                proc_state.describe_cert(never_def=True) + ' CRL issuer'
            )
        from pyhanko_certvalidator.validate import intl_validate_path
        new_stack = proc_state.cert_path_stack.cons(candidate_crl_issuer_path)
        await intl_validate_path(
            validation_context,
            candidate_crl_issuer_path,
            proc_state=ValProcState(
                ee_name_override=temp_override,
                cert_path_stack=new_stack
            )
        )

    except PathValidationError as e:
        iss_cert = candidate_crl_issuer_path.last
        logger.warning(
            f"Path for CRL issuer {iss_cert.subject.human_friendly} could not "
            f"be validated.", exc_info=e
        )
        raise CRLValidationError(
            f'The CRL issuer certificate path could not be validated. {e}'
        )


async def _find_candidate_crl_paths(
        crl_authority_name: x509.Name,
        certificate_list: crl.CertificateList,
        *, cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        cert_issuer_auth: Authority,
        cert_path: ValidationPath,
        certificate_registry: CertificateRegistry,
        is_indirect: bool, proc_state: ValProcState) \
        -> Tuple[List[ValidationPath], _CRLIssuerSearchErrs]:

    cert_sha256 = hashlib.sha256(cert.dump()).digest()

    candidate_crl_issuers = await _find_candidate_crl_issuer_certs(
        crl_authority_name, certificate_list,
        cert_issuer_auth=cert_issuer_auth,
        cert_registry=certificate_registry
    )
    cert_issuer_name = cert_issuer_auth.name

    errs = _CRLIssuerSearchErrs(candidate_issuers=len(candidate_crl_issuers))
    candidate_paths = []
    for candidate_crl_issuer in candidate_crl_issuers:
        direct_issuer = candidate_crl_issuer.subject == cert_issuer_name

        # In some cases an indirect CRL issuer is a certificate issued
        # by the certificate issuer. However, we need to ensure that
        # the candidate CRL issuer is not the certificate being checked,
        # otherwise we may be checking an incorrect CRL and produce
        # incorrect results.
        indirect_issuer = (
            candidate_crl_issuer.issuer == cert_issuer_name
            and candidate_crl_issuer.sha256 != cert_sha256
        )

        if not direct_issuer and not indirect_issuer and not is_indirect:
            errs.candidates_skipped += 1
            continue

        key_usage_value = candidate_crl_issuer.key_usage_value
        if key_usage_value and 'crl_sign' not in key_usage_value.native:
            errs.unauthorized_certs += 1
            continue

        try:
            # Step g
            # NOTE: Theoretically this can only be done after full X.509
            # path validation (step f), but that only matters for DSA key
            # inheritance which we don't support anyhow when doing revocation
            # checks.
            _verify_crl_signature(
                certificate_list, candidate_crl_issuer.public_key
            )
        except CRLValidationError:
            errs.signatures_failed += 1
            continue

        cand_path = proc_state.check_path_verif_recursion(candidate_crl_issuer)
        if not cand_path:
            try:
                cand_path = cert_path \
                    .truncate_to_issuer_and_append(candidate_crl_issuer)
            except LookupError:
                errs.path_building_failures += 1
                continue
        candidate_paths.append(cand_path)
    return candidate_paths, errs


async def _find_crl_issuer(
        crl_authority_name: x509.Name,
        certificate_list: crl.CertificateList,
        *, cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        cert_issuer_auth: Authority,
        cert_path: ValidationPath,
        validation_context: ValidationContext,
        is_indirect: bool,
        proc_state: ValProcState) -> ValidationPath:

    candidate_paths, errs = await _find_candidate_crl_paths(
        crl_authority_name, certificate_list,
        cert=cert, cert_issuer_auth=cert_issuer_auth,
        cert_path=cert_path,
        certificate_registry=validation_context.certificate_registry,
        is_indirect=is_indirect, proc_state=proc_state
    )

    for candidate_crl_issuer_path in candidate_paths:

        candidate_crl_issuer = candidate_crl_issuer_path.last

        # Skip path validation step if we're recursing
        #  (necessary to process CRLs that have their own certificate in-scope,
        #   which is questionable practice, but PKITS has a test case for this
        #   specific wrinkle, and it's not contradicted by anything in RFC 5280,
        #   so it's probably allowed in theory)
        if proc_state.check_path_verif_recursion(candidate_crl_issuer):
            validation_context.revinfo_manager \
                .record_crl_issuer(certificate_list, candidate_crl_issuer)
            return candidate_crl_issuer_path
        # Step f
        # Note: this is not the same as .truncate_to() if
        # candidate_crl_issuer doesn't appear in the path!
        candidate_crl_issuer_path = cert_path \
            .truncate_to_issuer_and_append(candidate_crl_issuer)
        try:
            # This check needs to know not only whether the names agree,
            # but also whether the keys are the same, in order to yield
            # the correct error message on failure.
            # (Scenario: CA with separate keys for CRL signing and for
            # certificate issuance, but with the same name on both certs)
            issuing_authority_identical = not is_indirect and (
                cert_issuer_auth is not None
                and cert_issuer_auth.public_key.dump()
                == candidate_crl_issuer.public_key.dump()
            )
            await _validate_crl_issuer_path(
                candidate_crl_issuer_path=candidate_crl_issuer_path,
                validation_context=validation_context,
                issuing_authority_identical=issuing_authority_identical,
                proc_state=proc_state
            )
            validation_context.revinfo_manager \
                .record_crl_issuer(certificate_list, candidate_crl_issuer)
            return candidate_crl_issuer_path
        except CRLValidationError as e:
            errs.explicit_errors.append(e)
            continue
    raise errs.get_exc()


@dataclass
class _CRLErrs:
    failures: list = field(default_factory=list)
    issuer_failures: int = 0


def _find_matching_delta_crl(delta_lists: List[CRLWithPOE],
                             crl_authority_name: x509.Name,
                             crl_idp: crl.IssuingDistributionPoint,
                             parent_crl_aki: Optional[bytes]) -> CRLWithPOE:
    for candidate_delta_cl_with_poe in delta_lists:
        candidate_delta_cl = candidate_delta_cl_with_poe.crl_data
        # Step c 1
        if candidate_delta_cl.issuer != crl_authority_name:
            continue

        # Step c 2
        delta_crl_idp = candidate_delta_cl.issuing_distribution_point_value
        if (crl_idp is None and delta_crl_idp is not None) or (
                crl_idp is not None and delta_crl_idp is None):
            continue

        if crl_idp is not None \
                and crl_idp.native != delta_crl_idp.native:
            continue

        # Step c 3
        if parent_crl_aki != candidate_delta_cl.authority_key_identifier:
            continue

        return candidate_delta_cl_with_poe


def _match_dps_idp_names(crl_idp: crl.IssuingDistributionPoint,
                         crl_dps: Optional[x509.CRLDistributionPoints],
                         crl_issuer: x509.Certificate,
                         crl_authority_name: x509.Name) -> bool:

    # Step b 2 i
    has_idp_name = False
    has_dp_name = False
    idp_dp_match = False

    idp_general_names = []
    idp_dp_name = crl_idp['distribution_point']
    if idp_dp_name:
        has_idp_name = True
        if idp_dp_name.name == 'full_name':
            for general_name in idp_dp_name.chosen:
                idp_general_names.append(general_name)
        else:
            inner_extended_issuer_name = crl_issuer.subject.copy()
            inner_extended_issuer_name.chosen.append(
                idp_dp_name.chosen.untag())
            idp_general_names.append(x509.GeneralName(
                name='directory_name',
                value=inner_extended_issuer_name
            ))

    if crl_dps:
        for dp in crl_dps:
            if idp_dp_match:
                break
            dp_name = dp['distribution_point']
            if dp_name:
                has_dp_name = True
                if dp_name.name == 'full_name':
                    for general_name in dp_name.chosen:
                        if general_name in idp_general_names:
                            idp_dp_match = True
                            break
                else:
                    inner_extended_issuer_name = crl_issuer.subject.copy()
                    inner_extended_issuer_name.chosen.append(
                        dp_name.chosen.untag())
                    dp_extended_issuer_name = x509.GeneralName(
                        name='directory_name',
                        value=inner_extended_issuer_name
                    )

                    if dp_extended_issuer_name in idp_general_names:
                        idp_dp_match = True

            elif dp['crl_issuer']:
                has_dp_name = True
                for dp_crl_authority_name in dp['crl_issuer']:
                    if dp_crl_authority_name in idp_general_names:
                        idp_dp_match = True
                        break
    else:
        # If there is no DP, we consider the CRL issuer name to be it
        has_dp_name = True
        general_name = x509.GeneralName(
            name='directory_name',
            value=crl_authority_name
        )
        if general_name in idp_general_names:
            idp_dp_match = True

    return idp_dp_match or not has_idp_name or not has_dp_name


def _handle_crl_idp_ext_constraints(cert: x509.Certificate,
                                    certificate_list: crl.CertificateList,
                                    crl_issuer: x509.Certificate,
                                    crl_idp: crl.IssuingDistributionPoint,
                                    crl_authority_name: x509.Name,
                                    errs: _CRLErrs) -> bool:
    match = _match_dps_idp_names(
        crl_idp=crl_idp, crl_dps=cert.crl_distribution_points_value,
        crl_issuer=crl_issuer,
        crl_authority_name=crl_authority_name,
    )
    if not match:
        errs.failures.append((
            pretty_message(
                '''
                The CRL issuing distribution point extension does not
                share any names with the certificate CRL distribution
                point extension
                '''
            ),
            certificate_list
        ))
        errs.issuer_failures += 1
        return False

    # Step b 2 ii
    if crl_idp['only_contains_user_certs'].native:
        if cert.basic_constraints_value and \
                cert.basic_constraints_value['ca'].native:
            errs.failures.append((
                pretty_message(
                    '''
                    CRL only contains end-entity certificates and
                    certificate is a CA certificate
                    '''
                ),
                certificate_list
            ))
            return False

    # Step b 2 iii
    if crl_idp['only_contains_ca_certs'].native:
        if not cert.basic_constraints_value or \
                cert.basic_constraints_value['ca'].native is False:
            errs.failures.append((
                pretty_message(
                    '''
                    CRL only contains CA certificates and certificate
                    is an end-entity certificate
                    '''
                ),
                certificate_list
            ))
            return False

    # Step b 2 iv
    if crl_idp['only_contains_attribute_certs'].native:
        errs.failures.append((
            'CRL only contains attribute certificates',
            certificate_list
        ))
        return False

    return True


def _handle_attr_cert_crl_idp_ext_constraints(
        certificate_list: crl.CertificateList,
        crl_dps: Optional[x509.CRLDistributionPoints],
        crl_issuer: x509.Certificate,
        crl_idp: crl.IssuingDistributionPoint,
        crl_authority_name: x509.Name,
        errs: _CRLErrs) -> bool:

    match = _match_dps_idp_names(
        crl_idp=crl_idp, crl_dps=crl_dps,
        crl_issuer=crl_issuer, crl_authority_name=crl_authority_name,
    )
    if not match:
        errs.failures.append((
            pretty_message(
                '''
                The CRL issuing distribution point extension does not
                share any names with the attribute certificate's
                CRL distribution point extension
                '''
            ),
            certificate_list
        ))
        errs.issuer_failures += 1
        return False

    # Step b 2 ii
    pkc_only = (
       crl_idp['only_contains_user_certs'].native
       or crl_idp['only_contains_ca_certs'].native
    )
    if pkc_only:
        errs.failures.append((
            pretty_message(
                '''
                CRL only contains public-key certificates, but
                certificate is an attribute certificate
                '''
            ),
            certificate_list
        ))
        return False

    return True


async def _handle_single_crl(
        cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        cert_issuer_auth: Authority,
        certificate_list_with_poe: CRLWithPOE,
        path: ValidationPath,
        validation_context: ValidationContext,
        delta_lists_by_issuer: Dict[str, List[CRLWithPOE]],
        use_deltas: bool, errs: _CRLErrs,
        proc_state: ValProcState) -> Optional[Set[str]]:

    certificate_list = certificate_list_with_poe.crl_data

    try:
        is_indirect, crl_authority_name = _get_crl_authority_name(
            certificate_list_with_poe, cert_issuer_auth.name,
            certificate_registry=validation_context.certificate_registry,
            errs=errs
        )
    except LookupError:
        # already logged by _get_crl_authority_name
        return None

    # check if we already know the issuer of this CRL
    crl_issuer = validation_context\
        .revinfo_manager.check_crl_issuer(certificate_list)
    # if not, attempt to determine it
    if not crl_issuer:
        try:
            crl_issuer_path = await _find_crl_issuer(
                crl_authority_name, certificate_list,
                cert=cert, cert_issuer_auth=cert_issuer_auth,
                cert_path=path,
                validation_context=validation_context,
                is_indirect=is_indirect,
                proc_state=proc_state
            )
            crl_issuer = crl_issuer_path.last
        except CRLNoMatchesError:
            # this no-match issue will be dealt with at a higher level later
            errs.issuer_failures += 1
            return None
        except (CertificateFetchError, CRLValidationError) as e:
            errs.failures.append((e.args[0], certificate_list))
            return None

    interim_reasons = _get_crl_scope_assuming_authority(
        crl_issuer=crl_issuer,
        cert=cert, certificate_list_with_poe=certificate_list_with_poe,
        is_indirect=is_indirect, errs=errs
    )

    if interim_reasons is None:
        return None

    freshness_result = certificate_list_with_poe.usable_at(
        policy=validation_context.revinfo_policy,
        timing_info=validation_context.timing_info
    )
    if freshness_result != RevinfoUsabilityRating.OK:
        if freshness_result == RevinfoUsabilityRating.STALE:
            msg = 'CRL is not recent enough'
        elif freshness_result == RevinfoUsabilityRating.TOO_NEW:
            msg = 'CRL is too recent'
        else:
            msg = 'CRL freshness could not be established'
        errs.failures.append((msg, certificate_list_with_poe))
        return None

    # Step c
    if use_deltas:
        delta_certificate_list_with_poe = _maybe_get_delta_crl(
            certificate_list=certificate_list, crl_issuer=crl_issuer,
            policy=validation_context.revinfo_policy,
            timing_info=validation_context.timing_info,
            delta_lists_by_issuer=delta_lists_by_issuer, errs=errs
        )
    else:
        delta_certificate_list_with_poe = None

    try:
        revoked_date, revoked_reason = _check_cert_on_crl_and_delta(
            crl_issuer=crl_issuer, cert=cert,
            certificate_list_with_poe=certificate_list_with_poe,
            delta_certificate_list_with_poe=delta_certificate_list_with_poe,
            errs=errs,
        )
    except NotImplementedError:
        # the subroutine already registered the failure, so just bail
        return None

    if revoked_reason:
        raise RevokedError.format(
            reason=revoked_reason, revocation_dt=revoked_date,
            revinfo_type='CRL', proc_state=proc_state,
        )
    return interim_reasons


def _get_crl_authority_name(
        certificate_list_with_poe: CRLWithPOE,
        cert_issuer_name: x509.Name,
        certificate_registry: CertificateRegistry,
        errs: _CRLErrs) -> Tuple[bool, x509.Name]:
    """
    Figure out the name of the entity on behalf of which the CRL was issued.
    """

    certificate_list = certificate_list_with_poe.crl_data

    crl_idp: crl.IssuingDistributionPoint \
        = certificate_list.issuing_distribution_point_value
    is_indirect = bool(crl_idp and crl_idp['indirect_crl'].native)
    if not is_indirect:
        crl_authority_name = certificate_list.issuer
    else:
        crl_idp_name = crl_idp['distribution_point']
        if crl_idp_name:
            if crl_idp_name.name == 'full_name':
                crl_authority_name = crl_idp_name.chosen[0].chosen
            else:
                crl_authority_name = cert_issuer_name.copy().chosen.append(
                    crl_idp_name.chosen
                )
        elif certificate_list.authority_key_identifier:
            tmp_crl_issuer = certificate_registry.retrieve_by_key_identifier(
                certificate_list.authority_key_identifier
            )
            crl_authority_name = tmp_crl_issuer.subject
        else:
            errs.failures.append((
                'CRL is marked as an indirect CRL, but provides no '
                'mechanism for locating the CRL issuer certificate',
                certificate_list_with_poe
            ))
            raise LookupError
    return is_indirect, crl_authority_name


def _maybe_get_delta_crl(
        certificate_list: crl.CertificateList,
        crl_issuer: x509.Certificate,
        delta_lists_by_issuer: Dict[str, List[CRLWithPOE]],
        errs: _CRLErrs,
        timing_info: Optional[ValidationTimingInfo],
        policy: CertRevTrustPolicy) -> Optional[CRLWithPOE]:

    if not certificate_list.freshest_crl_value \
            or len(certificate_list.freshest_crl_value) == 0:
        # nothing to do, return
        return None

    crl_authority_name = crl_issuer.subject
    crl_idp: crl.IssuingDistributionPoint \
        = certificate_list.issuing_distribution_point_value

    candidate_delta_lists = \
        delta_lists_by_issuer.get(crl_authority_name.hashable, [])
    delta_certificate_list_with_poe = _find_matching_delta_crl(
        delta_lists=candidate_delta_lists,
        crl_authority_name=crl_authority_name, crl_idp=crl_idp,
        parent_crl_aki=certificate_list.authority_key_identifier
    )
    if not delta_certificate_list_with_poe:
        return None

    delta_certificate_list = delta_certificate_list_with_poe.crl_data

    if delta_certificate_list.critical_extensions - KNOWN_CRL_EXTENSIONS:
        errs.failures.append((
            'One or more unrecognized critical extensions are present in '
            'the delta CRL',
            delta_certificate_list_with_poe
        ))
        return None

    # Step h
    try:
        _verify_crl_signature(delta_certificate_list, crl_issuer.public_key)
    except CRLValidationError:
        errs.failures.append((
            'Delta CRL signature could not be verified',
            delta_certificate_list_with_poe
        ))
        return None

    freshness_result = delta_certificate_list_with_poe.usable_at(
        policy=policy, timing_info=timing_info
    )
    if freshness_result != RevinfoUsabilityRating.OK:
        if freshness_result == RevinfoUsabilityRating.STALE:
            msg = 'Delta CRL is stale'
        elif freshness_result == RevinfoUsabilityRating.TOO_NEW:
            msg = 'Delta CRL is too recent'
        else:
            msg = 'Delta CRL freshness could not be established'
        errs.failures.append((msg, delta_certificate_list_with_poe))
        return None
    return delta_certificate_list_with_poe


def _get_crl_scope_assuming_authority(
        crl_issuer: x509.Certificate,
        cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        certificate_list_with_poe: CRLWithPOE,
        is_indirect: bool,
        errs: _CRLErrs) -> Optional[Set[str]]:

    certificate_list = certificate_list_with_poe.crl_data
    crl_idp: crl.IssuingDistributionPoint \
        = certificate_list.issuing_distribution_point_value

    is_pkc = isinstance(cert, x509.Certificate)
    # Step b 1
    has_dp_crl_issuer = False
    dp_match = False

    if is_pkc:
        crl_dps = cert.crl_distribution_points_value
    else:
        crl_dps = get_ac_extension_value(cert, 'crl_distribution_points')
    if crl_dps:
        crl_issuer_general_name = x509.GeneralName(
            name='directory_name',
            value=crl_issuer.subject
        )
        for dp in crl_dps:
            if dp['crl_issuer']:
                has_dp_crl_issuer = True
                if crl_issuer_general_name in dp['crl_issuer']:
                    dp_match = True

    crl_authority_name = crl_issuer.subject
    cert_issuer_name = get_issuer_dn(cert)
    same_issuer = crl_authority_name == cert_issuer_name
    indirect_match = has_dp_crl_issuer and dp_match and is_indirect
    missing_idp = has_dp_crl_issuer and (not dp_match or not is_indirect)
    indirect_crl_issuer = crl_issuer.issuer == cert_issuer_name

    if (not same_issuer and not indirect_match and not indirect_crl_issuer) \
            or missing_idp:
        errs.issuer_failures += 1
        return None

    # Step b 2

    if crl_idp is not None:
        if is_pkc:
            crl_idp_match = _handle_crl_idp_ext_constraints(
                cert=cert, certificate_list=certificate_list,
                crl_issuer=crl_issuer, crl_idp=crl_idp,
                crl_authority_name=crl_authority_name, errs=errs
            )
        else:
            crl_idp_match = _handle_attr_cert_crl_idp_ext_constraints(
                crl_dps=crl_dps, certificate_list=certificate_list,
                crl_issuer=crl_issuer, crl_idp=crl_idp,
                crl_authority_name=crl_authority_name, errs=errs
            )
        # error reporting is taken care of in the delegated method
        if not crl_idp_match:
            return None

    # Step d
    idp_reasons = None

    if crl_idp and crl_idp['only_some_reasons'].native is not None:
        idp_reasons = crl_idp['only_some_reasons'].native

    reason_keys = None
    if idp_reasons:
        reason_keys = idp_reasons

    if reason_keys is None:
        interim_reasons = VALID_REVOCATION_REASONS.copy()
    else:
        interim_reasons = reason_keys

    # Step e
    # We don't skip a CRL if it only contains reasons already checked since
    # a certificate issuer can self-issue a new cert that is used for CRLs

    if certificate_list.critical_extensions - KNOWN_CRL_EXTENSIONS:
        errs.failures.append((
            'One or more unrecognized critical extensions are present in '
            'the CRL',
            certificate_list_with_poe
        ))
        return None

    return interim_reasons


def _check_cert_on_crl_and_delta(
        crl_issuer: x509.Certificate,
        cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        certificate_list_with_poe: CRLWithPOE,
        delta_certificate_list_with_poe: Optional[CRLWithPOE],
        errs: _CRLErrs):

    certificate_list = certificate_list_with_poe.crl_data
    # Step i
    revoked_reason = None
    revoked_date = None

    cert_issuer_name = get_issuer_dn(cert)

    if delta_certificate_list_with_poe:
        delta_certificate_list = delta_certificate_list_with_poe.crl_data
        try:
            revoked_date, revoked_reason = \
                _find_cert_in_list(cert, cert_issuer_name,
                                   delta_certificate_list, crl_issuer)
        except NotImplementedError:
            errs.failures.append((
                'One or more unrecognized critical extensions are present in '
                'the CRL entry for the certificate',
                delta_certificate_list_with_poe
            ))
            raise

    # Step j
    if revoked_reason is None:
        try:
            revoked_date, revoked_reason = \
                _find_cert_in_list(cert, cert_issuer_name,
                                   certificate_list, crl_issuer)
        except NotImplementedError:
            errs.failures.append((
                'One or more unrecognized critical extensions are present in '
                'the CRL entry for the certificate',
                certificate_list_with_poe
            ))
            raise

    # Step k
    if revoked_reason and revoked_reason.native == 'remove_from_crl':
        revoked_reason = None
        revoked_date = None

    return revoked_date, revoked_reason


async def _classify_relevant_crls(
        revinfo_manager: RevinfoManager,
        cert: x509.Certificate, errs: _CRLErrs):

    certificate_lists = await revinfo_manager.async_retrieve_crls_with_poe(cert)

    complete_lists_by_issuer = defaultdict(list)
    delta_lists_by_issuer = defaultdict(list)
    for certificate_list_with_poe in certificate_lists:
        certificate_list = certificate_list_with_poe.crl_data
        try:
            issuer_hashable = certificate_list.issuer.hashable
            if certificate_list.delta_crl_indicator_value is None:
                complete_lists_by_issuer[issuer_hashable] \
                    .append(certificate_list_with_poe)
            else:
                delta_lists_by_issuer[issuer_hashable].append(
                    certificate_list_with_poe
                )
        except ValueError as e:
            msg = "Generic processing error while classifying CRL."
            logging.debug(msg, exc_info=e)
            errs.failures.append((msg, certificate_list))
    return complete_lists_by_issuer, delta_lists_by_issuer


def _process_crl_completeness(checked_reasons: Set[str], total_crls: int,
                              errs: _CRLErrs, proc_state: ValProcState):

    # CRLs should not include this value, but at least one of the examples
    # from the NIST test suite does
    checked_reasons -= {'unused'}

    if checked_reasons != VALID_REVOCATION_REASONS:
        if total_crls == errs.issuer_failures:
            return CRLNoMatchesError(pretty_message(
                '''
                No CRLs were issued by the issuer of %s, or any indirect CRL
                issuer
                ''',
                proc_state.describe_cert()
            ))

        if not errs.failures:
            errs.failures.append((
                'The available CRLs do not cover all revocation reasons',
            ))

        return CRLValidationIndeterminateError(
            pretty_message(
                '''
                Unable to determine if %s is revoked due to insufficient
                information from known CRLs
                ''',
                proc_state.describe_cert()
            ),
            errs.failures
        )


async def verify_crl(
        cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        path: ValidationPath,
        validation_context: ValidationContext, use_deltas=True,
        proc_state: Optional[ValProcState] = None):
    """
    Verifies a certificate against a list of CRLs, checking to make sure the
    certificate has not been revoked. Uses the algorithm from
    https://tools.ietf.org/html/rfc5280#section-6.3 as a basis, but the
    implementation differs to allow CRLs from unrecorded locations.

    :param cert:
        An asn1crypto.x509.Certificate or asn1crypto.cms.AttributeCertificateV2
        object to check for in the CRLs

    :param path:
        A pyhanko_certvalidator.path.ValidationPath object of the cert's
        validation path, or in the case of an AC, the AA's validation path.

    :param validation_context:
        A pyhanko_certvalidator.context.ValidationContext object to use for caching
        validation information

    :param use_deltas:
        A boolean indicating if delta CRLs should be used

    :param proc_state:
        Internal state for error reporting and policy application decisions.

    :raises:
        pyhanko_certvalidator.errors.CRLNoMatchesError - when none of the CRLs match the certificate
        pyhanko_certvalidator.errors.CRLValidationError - when any error occurs trying to verify the CertificateList
        pyhanko_certvalidator.errors.RevokedError - when the CRL indicates the certificate has been revoked
    """

    is_pkc = isinstance(cert, x509.Certificate)
    proc_state = proc_state or ValProcState(
        cert_path_stack=ConsList.sing(path),
        ee_name_override="attribute certificate" if not is_pkc else None
    )

    revinfo_manager = validation_context.revinfo_manager
    errs = _CRLErrs()
    complete_lists_by_issuer, delta_lists_by_issuer = \
        await _classify_relevant_crls(revinfo_manager, cert, errs)

    try:
        cert_issuer_auth = path.find_issuing_authority(cert)
    except LookupError:
        raise CRLNoMatchesError(pretty_message(
            '''
            Could not determine issuer certificate for %s in path.
            ''',
            proc_state.describe_cert()
        ))

    # In the main loop, only complete CRLs are processed, so delta CRLs are
    # weeded out of the to-do list
    crls_to_process = []
    for issuer_crls in complete_lists_by_issuer.values():
        crls_to_process.extend(issuer_crls)
    total_crls = len(crls_to_process)

    checked_reasons = set()

    while len(crls_to_process) > 0:
        certificate_list_with_poe = crls_to_process.pop(0)
        try:
            interim_reasons = await _handle_single_crl(
                cert=cert, cert_issuer_auth=cert_issuer_auth,
                certificate_list_with_poe=certificate_list_with_poe,
                path=path, validation_context=validation_context,
                delta_lists_by_issuer=delta_lists_by_issuer,
                use_deltas=use_deltas, errs=errs,
                proc_state=proc_state
            )
            if interim_reasons is not None:
                # Step l
                checked_reasons |= interim_reasons
        except ValueError as e:
            msg = "Generic processing error while validating CRL."
            logging.debug(msg, exc_info=e)
            errs.failures.append((msg, certificate_list_with_poe))

    exc = _process_crl_completeness(
        checked_reasons, total_crls, errs, proc_state
    )
    if exc is not None:
        raise exc


def _verify_crl_signature(certificate_list, public_key):
    """
    Verifies the digital signature on an asn1crypto.crl.CertificateList object

    :param certificate_list:
        An asn1crypto.crl.CertificateList object

    :raises:
        pyhanko_certvalidator.errors.CRLValidationError - when the signature is
        invalid or uses an unsupported algorithm
    """

    signature_algo = certificate_list['signature_algorithm'].signature_algo
    hash_algo = certificate_list['signature_algorithm'].hash_algo

    try:
        validate_sig(
            signature=certificate_list['signature'].native,
            signed_data=certificate_list['tbs_cert_list'].dump(),
            public_key_info=public_key,
            sig_algo=signature_algo, hash_algo=hash_algo,
            parameters=certificate_list['signature_algorithm']['parameters']
        )
    except PSSParameterMismatch as e:
        raise CRLValidationError(
            'Invalid signature parameters on CertificateList'
        ) from e
    except InvalidSignature:
        raise CRLValidationError(
            'Unable to verify the signature of the CertificateList'
        )


def _find_cert_in_list(
        cert: Union[x509.Certificate, cms.AttributeCertificateV2],
        cert_issuer_name: x509.Name,
        certificate_list: crl.CertificateList,
        crl_issuer: x509.Certificate):
    """
    Looks for a cert in the list of revoked certificates

    :param cert:
        An asn1crypto.x509.Certificate object of the cert being checked,
        or an asn1crypto.cms.AttributeCertificateV2 object in the case
        of an attribute certificate.

    :param cert_issuer_name:
        The certificate issuer's distinguished name

    :param certificate_list:
        An ans1crypto.crl.CertificateList object to look in for the cert

    :param crl_issuer:
        An asn1crypto.x509.Certificate object of the CRL issuer

    :return:
        A tuple of (None, None) if not present, otherwise a tuple of
        (asn1crypto.x509.Time object, asn1crypto.crl.CRLReason object)
        representing the date/time the object was revoked and why
    """

    revoked_certificates \
        = certificate_list['tbs_cert_list']['revoked_certificates']

    if isinstance(cert, x509.Certificate):
        cert_serial = cert.serial_number
    else:
        cert_serial = cert['ac_info']['serial_number'].native

    last_issuer_name = crl_issuer.subject
    for revoked_cert in revoked_certificates:
        # If any unknown critical extensions, the entry can not be used
        if revoked_cert.critical_extensions - KNOWN_CRL_ENTRY_EXTENSIONS:
            raise NotImplementedError()

        if revoked_cert.issuer_name and \
                revoked_cert.issuer_name != last_issuer_name:
            last_issuer_name = revoked_cert.issuer_name
        if last_issuer_name != cert_issuer_name:
            continue

        if revoked_cert['user_certificate'].native != cert_serial:
            continue

        if not revoked_cert.crl_reason_value:
            crl_reason = crl.CRLReason('unspecified')
        else:
            crl_reason = revoked_cert.crl_reason_value

        return revoked_cert['revocation_date'].native, crl_reason

    return None, None
