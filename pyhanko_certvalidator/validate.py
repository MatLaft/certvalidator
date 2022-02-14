# coding: utf-8

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Set, Dict

from asn1crypto import x509, cms, core
from asn1crypto.x509 import Validity
from cryptography.exceptions import InvalidSignature

from ._eddsa_oids import register_eddsa_oids
from ._state import ValProcState
from .asn1_types import AAControls, Target as Target
from .context import (
    ValidationContext,
    ACTargetDescription
)
from .policy_decl import PKIXValidationParams, RevocationCheckingRule, \
    intersect_policy_sets
from .name_trees import PermittedSubtrees, ExcludedSubtrees, \
    process_general_subtrees, default_permitted_subtrees, \
    default_excluded_subtrees
from .errors import (
    CRLNoMatchesError,
    CRLValidationIndeterminateError,
    CRLFetchError,
    InvalidCertificateError,
    OCSPNoMatchesError,
    OCSPValidationIndeterminateError,
    OCSPFetchError,
    ValidationError,
    PathValidationError,
    PathBuildingError, InvalidAttrCertificateError, NotYetValidError,
    ExpiredError, WeakAlgorithmError, PSSParameterMismatch,
    InsufficientRevinfoError,
)
from .path import ValidationPath, QualifiedPolicy
from .policy_tree import (
    PolicyTreeRoot,
    PolicyTreeNode,
    update_policy_tree,
    prune_unacceptable_policies,
    apply_policy_mapping,
    enumerate_policy_mappings
)

from .registry import CertificateCollection
from .revinfo.validate_crl import verify_crl
from .revinfo.validate_ocsp import verify_ocsp_response
from .authority import CertTrustAnchor, TrustAnchor
from .util import (
    extract_dir_name,
    get_ac_extension_value,
    get_declared_revinfo,
    validate_sig,
    pretty_message, ConsList
)

logger = logging.getLogger(__name__)

# make sure EdDSA OIDs are known to asn1crypto
register_eddsa_oids()


def validate_path(validation_context, path,
                  parameters: PKIXValidationParams = None):
    """
    Validates the path using the algorithm from
    https://tools.ietf.org/html/rfc5280#section-6.1.

    Critical extensions on the end-entity certificate are not validated
    and are left up to the consuming application to process and/or fail on.

    .. note::
        This is a synchronous equivalent of :func:`.async_validate_path` that
        calls the latter in a new event loop. As such, it can't be used
        from within asynchronous code.

    :param validation_context:
        A pyhanko_certvalidator.context.ValidationContext object to use for
        configuring validation behavior

    :param path:
        A pyhanko_certvalidator.path.ValidationPath object of the path to validate

    :param parameters:
        Additional input parameters to the PKIX validation algorithm.
        These are not used when validating CRLs and OCSP responses.

    :raises:
        pyhanko_certvalidator.errors.PathValidationError - when an error occurs validating the path
        pyhanko_certvalidator.errors.RevokedError - when the certificate or another certificate in its path has been revoked

    :return:
        The final certificate in the path - an instance of
        asn1crypto.x509.Certificate
    """

    result = asyncio.run(
        async_validate_path(validation_context, path, parameters=parameters)
    )
    return result


async def async_validate_path(validation_context, path: ValidationPath,
                              parameters: PKIXValidationParams = None):
    """
    Validates the path using the algorithm from
    https://tools.ietf.org/html/rfc5280#section-6.1.

    Critical extensions on the end-entity certificate are not validated
    and are left up to the consuming application to process and/or fail on.

    :param validation_context:
        A pyhanko_certvalidator.context.ValidationContext object to use for
        configuring validation behavior

    :param path:
        A pyhanko_certvalidator.path.ValidationPath object of the path to validate

    :param parameters:
        Additional input parameters to the PKIX validation algorithm.
        These are not used when validating CRLs and OCSP responses.

    :raises:
        pyhanko_certvalidator.errors.PathValidationError - when an error occurs validating the path
        pyhanko_certvalidator.errors.RevokedError - when the certificate or another certificate in its path has been revoked

    :return:
        The final certificate in the path - an instance of
        asn1crypto.x509.Certificate
    """

    proc_state = ValProcState(cert_path_stack=ConsList.sing(path))
    return await intl_validate_path(
        validation_context, path, parameters=parameters,
        proc_state=proc_state
    )


def validate_tls_hostname(validation_context: ValidationContext,
                          cert: x509.Certificate, hostname: str):
    """
    Validates the end-entity certificate from a
    pyhanko_certvalidator.path.ValidationPath object to ensure that the certificate
    is valid for the hostname provided and that the certificate is valid for
    the purpose of a TLS connection.

    THE CERTIFICATE PATH MUST BE VALIDATED SEPARATELY VIA validate_path()!

    :param validation_context:
        A pyhanko_certvalidator.context.ValidationContext object to use for
        configuring validation behavior

    :param cert:
        An asn1crypto.x509.Certificate object returned from validate_path()

    :param hostname:
        A unicode string of the TLS server hostname

    :raises:
        pyhanko_certvalidator.errors.InvalidCertificateError - when the certificate is not valid for TLS or the hostname
    """

    if validation_context.is_whitelisted(cert):
        return

    if not cert.is_valid_domain_ip(hostname):
        raise InvalidCertificateError(pretty_message(
            '''
            The X.509 certificate provided is not valid for %s. Valid hostnames
            include: %s
            ''',
            hostname,
            ', '.join(cert.valid_domains)
        ))

    bad_key_usage = cert.key_usage_value and 'digital_signature' not in cert.key_usage_value.native
    bad_ext_key_usage = cert.extended_key_usage_value and 'server_auth' not in cert.extended_key_usage_value.native

    if bad_key_usage or bad_ext_key_usage:
        raise InvalidCertificateError(pretty_message(
            '''
            The X.509 certificate provided is not valid for securing TLS
            connections
            '''
        ))


def validate_usage(validation_context: ValidationContext,
                   cert: x509.Certificate,
                   key_usage: Set[str],
                   extended_key_usage: Set[str],
                   extended_optional: bool):
    """
    Validates the end-entity certificate from a
    pyhanko_certvalidator.path.ValidationPath object to ensure that the certificate
    is valid for the key usage and extended key usage purposes specified.

    THE CERTIFICATE PATH MUST BE VALIDATED SEPARATELY VIA validate_path()!

    :param validation_context:
        A pyhanko_certvalidator.context.ValidationContext object to use for
        configuring validation behavior

    :param cert:
        An asn1crypto.x509.Certificate object returned from validate_path()

    :param key_usage:
        A set of unicode strings of the required key usage purposes

    :param extended_key_usage:
        A set of unicode strings of the required extended key usage purposes

    :param extended_optional:
        A bool - if the extended_key_usage extension may be omitted and still
        considered valid

    :raises:
        pyhanko_certvalidator.errors.InvalidCertificateError - when the certificate is not valid for the usages specified
    """

    if validation_context.is_whitelisted(cert):
        return

    if key_usage is None:
        key_usage = set()

    if extended_key_usage is None:
        extended_key_usage = set()

    missing_key_usage = key_usage
    if cert.key_usage_value:
        missing_key_usage = key_usage - cert.key_usage_value.native

    missing_extended_key_usage = set()
    if extended_optional is False and not cert.extended_key_usage_value:
        missing_extended_key_usage = extended_key_usage
    elif cert.extended_key_usage_value is not None:
        missing_extended_key_usage = extended_key_usage - set(cert.extended_key_usage_value.native)

    if missing_key_usage or missing_extended_key_usage:
        plural = 's' if len(missing_key_usage | missing_extended_key_usage) > 1 else ''
        friendly_purposes = []
        for purpose in sorted(missing_key_usage | missing_extended_key_usage):
            friendly_purposes.append(purpose.replace('_', ' '))
        raise InvalidCertificateError(pretty_message(
            '''
            The X.509 certificate provided is not valid for the purpose%s of %s
            ''',
            plural,
            ', '.join(friendly_purposes)
        ))


def validate_aa_usage(validation_context: ValidationContext,
                      cert: x509.Certificate,
                      extended_key_usage: Optional[Set[str]] = None):
    """
    Validate AA certificate profile conditions in RFC 5755 § 4.5

    :param validation_context:
    :param cert:
    :param extended_key_usage:
    :return:
    """
    if validation_context.is_whitelisted(cert):
        return

    # Check key usage requirements
    validate_usage(
        validation_context, cert, key_usage={'digital_signature'},
        extended_key_usage=extended_key_usage or set(),
        extended_optional=extended_key_usage is not None
    )

    # Check basic constraints: AA must not be a CA
    bc = cert.basic_constraints_value
    if bc is not None and bool(bc['ca']):
        raise InvalidCertificateError(pretty_message(
            '''
            The X.509 certificate provided is a CA certificate, so
            it cannot be used to validate attribute certificates.
            '''
        ))


def _validate_ac_targeting(attr_cert: cms.AttributeCertificateV2,
                           acceptable_targets: ACTargetDescription):

    target_info = get_ac_extension_value(attr_cert, 'target_information')
    if target_info is None:
        return

    target: Target
    for targets in target_info:
        for target in targets:
            if target.name == 'target_name':
                gen_name: x509.GeneralName = target.chosen
                valid_names = acceptable_targets.validator_names
            elif target.name == 'target_group':
                gen_name: x509.GeneralName = target.chosen
                valid_names = acceptable_targets.group_memberships
            else:
                logger.info(
                    f"'{target.name}' is not supported as a targeting mode; "
                    f"ignoring."
                )
                continue
            try:
                target_ok = gen_name in valid_names
            except ValueError:
                # fall back to binary comparison in case the name type is not
                # supported by asn1crypto's comparison logic for GeneralName
                #  (we could be more efficient here, but this is probably
                #   rare, so let's follow YAGNI)
                target_ok = gen_name.dump() in {n.dump() for n in valid_names}
            if target_ok:
                return

    # TODO log audit identity
    raise InvalidAttrCertificateError("AC targeting check failed")


SUPPORTED_AC_EXTENSIONS = frozenset([
    'authority_information_access',
    'authority_key_identifier',
    'crl_distribution_points',
    'freshest_crl',
    'key_identifier',
    'no_rev_avail',
    'target_information',
    # NOTE: we don't actively process this extension, but we never log holder
    # identifying information, so the purpose of the audit identity
    # extension is still satisfied.
    # TODO actually use audit_identity for logging purposes, falling back
    #  to holder info if audit_identity is not available.
    'audit_identity'
])


def _parse_iss_serial(iss_serial: cms.IssuerSerial, err_msg_prefix: str) \
        -> bytes:
    """
    Render a cms.IssuerSerial value into something that matches
    x509.Certificate.issuer_serial output.
    """
    issuer_names = iss_serial['issuer']
    issuer_dirname = extract_dir_name(issuer_names, err_msg_prefix)
    result_bytes = b'%s:%d' % (
        issuer_dirname.sha256, iss_serial['serial'].native
    )
    return result_bytes


def _process_aki_ext(aki_ext: x509.AuthorityKeyIdentifier):

    aki = aki_ext['key_identifier'].native  # could be None
    auth_iss_ser = auth_iss_dirname = None
    if not isinstance(aki_ext['authority_cert_issuer'], core.Void):
        auth_iss_dirname = extract_dir_name(
            aki_ext['authority_cert_issuer'],
            "Could not decode authority issuer in AKI extension"
        )
        auth_ser = aki_ext['authority_cert_serial_number'].native
        if auth_ser is not None:
            auth_iss_ser = b'%s:%d' % (auth_ser.sha256, auth_ser)

    return aki, auth_iss_dirname, auth_iss_ser


def _candidate_ac_issuers(attr_cert: cms.AttributeCertificateV2,
                          registry: CertificateCollection):
    # TODO support matching against subjectAltName?
    #  Outside the scope of RFC 5755, but it might make sense

    issuer_rec = attr_cert['ac_info']['issuer']
    aa_names: Optional[x509.GeneralNames] = None
    aa_iss_serial: Optional[bytes] = None
    if issuer_rec.name == 'v1_form':
        aa_names = issuer_rec.chosen
    else:
        issuerv2: cms.V2Form = issuer_rec.chosen
        if not isinstance(issuerv2['issuer_name'], core.Void):
            aa_names = issuerv2['issuer_name']
        if not isinstance(issuerv2['base_certificate_id'], core.Void):
            # not allowed by RFC 5755, but let's parse it anyway if
            # we encounter it
            aa_iss_serial = _parse_iss_serial(
                issuerv2['base_certificate_id'],
                "Could not identify AA issuer in base_certificate_id"
            )
        if not isinstance(issuerv2['object_digest_info'], core.Void):
            # TODO support objectdigestinfo? Also not allowed by RFC 5755
            raise NotImplementedError(
                "Could not identify AA; objectDigestInfo is not supported."
            )

    # Process the AKI extension if there is one
    aki_ext = get_ac_extension_value(attr_cert, 'authority_key_identifier')
    if aki_ext is not None:
        aki, aa_issuer, aki_aa_iss_serial = _process_aki_ext(aki_ext)
        if aki_aa_iss_serial is not None:
            if aa_iss_serial is not None and aa_iss_serial != aki_aa_iss_serial:
                raise InvalidAttrCertificateError(
                    "AC's AKI extension and issuer include conflicting "
                    "identifying information for the issuing AA"
                )
            else:
                aa_iss_serial = aki_aa_iss_serial
    else:
        aki = None

    candidates = ()
    aa_name = None
    if aa_names is not None:
        aa_name = extract_dir_name(aa_names, "Could not identify AA by name")
    if aa_iss_serial is not None:
        exact_cert = registry.retrieve_by_issuer_serial(aa_iss_serial)
        if exact_cert is not None:
            candidates = (exact_cert,)
    elif aa_name is not None:
        candidates = registry.retrieve_by_name(aa_name)

    for aa_candidate in candidates:
        if aa_name is not None and aa_candidate.subject != aa_name:
            continue
        if aki is not None and aa_candidate.key_identifier != aki:
            # AC's AKI doesn't match candidate's SKI
            continue
        yield aa_candidate


def _check_ac_signature(attr_cert: cms.AttributeCertificateV2,
                        aa_cert: x509.Certificate,
                        validation_context: ValidationContext):

    sd_algo = attr_cert['signature_algorithm']
    embedded_sd_algo = attr_cert['ac_info']['signature']
    if sd_algo.native != embedded_sd_algo.native:
        raise InvalidAttrCertificateError(pretty_message(
            '''
            Signature algorithm declaration in signed portion of AC does not
            match the signature algorithm declaration on the envelope.
            '''
        ))

    signature_algo = sd_algo.signature_algo
    hash_algo = attr_cert['signature_algorithm'].hash_algo

    if hash_algo in validation_context.weak_hash_algos:
        raise WeakAlgorithmError(pretty_message(
            '''
            The attribute certificate could not be validated because 
            the signature uses the weak hash algorithm %s
            ''',
            hash_algo
        ), is_ee_cert=True, is_side_validation=False)

    try:
        validate_sig(
            signature=attr_cert['signature'].native,
            signed_data=attr_cert['ac_info'].dump(),
            # TODO support PK parameter inheritance?
            #  (would have to remember the working public key from the
            #  validation algo)
            # low-priority since this only affects DSA in practice
            public_key_info=aa_cert.public_key,
            sig_algo=signature_algo, hash_algo=hash_algo,
            parameters=attr_cert['signature_algorithm']['parameters']
        )
    except PSSParameterMismatch:
        raise InvalidAttrCertificateError(pretty_message(
            '''
            The signature parameters for the attribute certificate
            do not match the constraints on the public key.
            '''
        ))
    except InvalidSignature:
        raise InvalidAttrCertificateError(pretty_message(
            '''
            The attribute certificate could not be validated because the
            signature could not be verified.
            ''',
        ))


def check_ac_holder_match(holder_cert: x509.Certificate, holder: cms.Holder):
    """
    Match a candidate holder certificate against the holder entry of an
    attribute certificate.

    :param holder_cert:
        Candidate holder certificate.
    :param holder:
        Holder value to match against.
    :return:
        Return the parts of the holder entry that mismatched as a set.
        Possible values are `'base_certificate_id'`, `'entity_name'` and
        `'object_digest_info'`.
        If the returned set is empty, all entries in the holder entry
        matched the information in the certificate.
    """

    base_cert_id = holder['base_certificate_id']
    mismatches = set()
    # TODO what about subjectAltName matches?

    if not isinstance(base_cert_id, core.Void):
        # repurpose _parse_iss_serial since RFC 5755 restricts
        # baseCertificateID.issuer to a single DN
        designated_iss_serial = _parse_iss_serial(
            base_cert_id, "Could not identify holder certificate issuer"
        )
        if designated_iss_serial != holder_cert.issuer_serial:
            mismatches.add('base_certificate_id')

    entity_name = holder['entity_name']
    # TODO what about subjectAltName matches?
    if not isinstance(entity_name, core.Void):
        holder_dn = extract_dir_name(
            entity_name,
            "Could not identify AC holder DN"
        )
        if holder_dn != holder_cert.subject:
            mismatches.add('entity_name')

    # TODO implement objectDigestInfo support
    obj_digest_info = holder['object_digest_info']
    if not isinstance(obj_digest_info, core.Void):
        raise NotImplementedError(
            "Object digest info is currently not supported"
        )
    return mismatches


@dataclass(frozen=True)
class ACValidationResult:
    """
    The result of a successful attribute certificate validation.
    """

    attr_cert: cms.AttributeCertificateV2
    """
    The attribute certificate that was validated.
    """

    aa_cert: x509.Certificate
    """
    The attribute authority that issued the certificate.
    """

    aa_path: ValidationPath
    """
    The validation path of the attribute authority's certificate.
    """

    approved_attributes: Dict[str, cms.AttCertAttribute]
    """
    Approved attributes in the attribute certificate, possibly filtered by
    AA controls.
    """


async def async_validate_ac(
        attr_cert: cms.AttributeCertificateV2,
        validation_context: ValidationContext,
        aa_pkix_params: PKIXValidationParams = PKIXValidationParams(),
        holder_cert: Optional[x509.Certificate] = None) -> ACValidationResult:
    """
    Validate an attribute certificate with respect to a given validation
    context.

    :param attr_cert:
        The attribute certificate to validate.
    :param validation_context:
        The validation context to validate against.
    :param aa_pkix_params:
        PKIX validation parameters to supply to the path validation algorithm
        applied to the attribute authority's certificate.
    :param holder_cert:
        Certificate of the presumed holder to match against the AC's holder
        entry. If not provided, the holder check is left to the caller to
        perform.

        .. note::
            This is a convenience option in case there's only one reasonable
            candidate holder certificate (e.g. when the attribute certificates
            are part of a CMS SignedData value with only a single signer).
    :return:
        An :class:`.ACValidationResult` detailing the validation result,
        if successful.
    """

    # Process extensions
    # We do this first because all later steps may involve potentially slow
    #  network IO, so this allows quicker failure.
    extensions_present = {
        ext['extn_id'].native: bool(ext['critical'])
        for ext in attr_cert['ac_info']['extensions']
    }
    unsupported_critical_extensions = {
        ext for ext, crit in extensions_present.items()
        if crit and ext not in SUPPORTED_AC_EXTENSIONS
    }
    if unsupported_critical_extensions:
        raise PathValidationError(pretty_message(
            '''
            The AC could not be validated because it contains the
            following unsupported critical extension%s: %s
            ''',
            's' if len(unsupported_critical_extensions) != 1 else '',
            ', '.join(sorted(unsupported_critical_extensions)),
        ), is_ee_cert=True, is_side_validation=False)
    if 'target_information' in extensions_present:
        targ_desc = validation_context.acceptable_ac_targets
        if targ_desc is None:
            raise InvalidAttrCertificateError(pretty_message(
                '''
                The attribute certificate is targeted, but no targeting
                information is available in the validation context.
                '''
            ))
        _validate_ac_targeting(attr_cert, targ_desc)

    ac_holder = attr_cert['ac_info']['holder']
    if len(ac_holder) == 0:
        raise InvalidAttrCertificateError("AC holder entry is empty")

    if holder_cert is not None:
        mismatches = check_ac_holder_match(holder_cert, ac_holder)
        if mismatches:
            raise InvalidAttrCertificateError(
                f"Could not match AC holder entry against supplied holder "
                f"certificate; mismatched entries: {', '.join(mismatches)}"
            )

    registry = validation_context.certificate_registry
    aa_candidates = _candidate_ac_issuers(attr_cert, registry)

    exceptions = []
    aa_path: Optional[ValidationPath] = None
    for aa_candidate in aa_candidates:
        try:
            validate_aa_usage(validation_context, aa_candidate)
        except InvalidAttrCertificateError as e:
            exceptions.append(e)
            continue
        try:
            paths = await registry.async_build_paths(aa_candidate)
        except PathBuildingError as e:
            exceptions.append(e)
            continue

        for candidate_path in paths:
            try:
                await intl_validate_path(
                    validation_context, candidate_path,
                    parameters=aa_pkix_params,
                    proc_state=ValProcState(
                        cert_path_stack=ConsList.sing(candidate_path),
                        ee_name_override="AA certificate"
                    )
                )
                aa_path = candidate_path
                break
            except ValidationError as e:
                exceptions.append(e)

    if aa_path is None:
        # TODO log audit identifier
        if not exceptions:
            raise PathBuildingError(
                "Could not find a suitable AA for the attribute certificate"
            )
        else:
            raise exceptions[0]

    # check the signature
    aa_cert = aa_path.last
    _check_ac_signature(attr_cert, aa_cert, validation_context)

    validity = attr_cert['ac_info']['att_cert_validity_period']
    # NOTE: this is a bit of a hack, and the path in question is only used
    #  for error reporting
    # TODO make paths with ACs at the end easier to handle
    ac_path = aa_path.copy_and_append(attr_cert)
    proc_state = ValProcState(
        cert_path_stack=ConsList.sing(ac_path),
        is_side_validation=False,
        ee_name_override="the attribute certificate"
    )
    _check_validity(
        validity=Validity({
            'not_before': validity['not_before_time'],
            'not_after': validity['not_after_time'],
        }),
        moment=validation_context.moment,
        tolerance=validation_context.time_tolerance,
        proc_state=proc_state,
    )
    if 'no_rev_avail' not in extensions_present:
        await _check_revocation(
            attr_cert, validation_context, ac_path,
            proc_state=proc_state
        )

    ok_attrs = {
        attr['type'].native: attr
        for attr in attr_cert['ac_info']['attributes']
        if aa_path.aa_attr_in_scope(attr['type'])
    }

    return ACValidationResult(
        attr_cert=attr_cert, aa_cert=aa_cert,
        aa_path=aa_path, approved_attributes=ok_attrs
    )


@dataclass
class _PathValidationState:
    """
    State variables that need to be maintained while traversing a certification
    path
    """

    valid_policy_tree: Optional['PolicyTreeRoot']
    explicit_policy: int
    inhibit_any_policy: int
    policy_mapping: int
    max_path_length: int
    max_aa_path_length: int
    working_public_key: x509.PublicKeyInfo
    working_issuer_name: x509.Name
    permitted_subtrees: PermittedSubtrees
    excluded_subtrees: ExcludedSubtrees
    aa_controls_used: bool = False

    @staticmethod
    def init_pkix_validation_state(
            path_length,
            trust_anchor: TrustAnchor,
            parameters: Optional[PKIXValidationParams]):

        trust_anchor_quals = trust_anchor.trust_qualifiers
        max_path_length = max_aa_path_length = path_length
        if trust_anchor_quals.max_path_length is not None:
            max_path_length = trust_anchor_quals.max_path_length
        if trust_anchor_quals.max_path_length is not None:
            max_aa_path_length = trust_anchor_quals.max_aa_path_length
        trust_anchor_params = trust_anchor_quals.standard_parameters

        if parameters is not None and trust_anchor_params is not None:
            # need to make sure both sets of parameters are respected
            acceptable_policies = intersect_policy_sets(
                parameters.user_initial_policy_set,
                trust_anchor_params.user_initial_policy_set
            )
            initial_any_policy_inhibit = \
                parameters.initial_any_policy_inhibit \
                and parameters.initial_any_policy_inhibit

            initial_explicit_policy = \
                parameters.initial_explicit_policy \
                and parameters.initial_explicit_policy

            initial_policy_mapping_inhibit = (
                parameters.initial_policy_mapping_inhibit
                and parameters.initial_policy_mapping_inhibit
            )
            initial_permitted_subtrees = PermittedSubtrees(
                parameters.initial_permitted_subtrees
                or default_permitted_subtrees()
            )
            if trust_anchor_params.initial_permitted_subtrees is not None:
                initial_permitted_subtrees.intersect_with(
                    trust_anchor_params.initial_permitted_subtrees
                )
            initial_excluded_subtrees = ExcludedSubtrees(
                parameters.initial_excluded_subtrees
                or default_excluded_subtrees()
            )
            if trust_anchor_params.initial_excluded_subtrees is not None:
                initial_excluded_subtrees.union_with(
                    trust_anchor_params.initial_excluded_subtrees
                )
        else:
            parameters = parameters or trust_anchor_params \
                         or PKIXValidationParams()
            acceptable_policies = parameters.user_initial_policy_set
            initial_explicit_policy = parameters.initial_explicit_policy
            initial_any_policy_inhibit = parameters.initial_any_policy_inhibit
            initial_policy_mapping_inhibit = \
                parameters.initial_policy_mapping_inhibit
            initial_permitted_subtrees = PermittedSubtrees(
                parameters.initial_permitted_subtrees
                or default_permitted_subtrees()
            )
            initial_excluded_subtrees = ExcludedSubtrees(
                parameters.initial_excluded_subtrees
                or default_excluded_subtrees()
            )

        state = _PathValidationState(
            # Step 1 a
            valid_policy_tree=PolicyTreeRoot.init_policy_tree(
                'any_policy', set(), {'any_policy'}
            ),
            # Steps 1 b-c
            permitted_subtrees=initial_permitted_subtrees,
            excluded_subtrees=initial_excluded_subtrees,
            # Steps 1 d-f
            explicit_policy=(
                0 if initial_explicit_policy
                else path_length + 1
            ),
            inhibit_any_policy=(
                0 if initial_any_policy_inhibit
                else path_length + 1
            ),
            policy_mapping=(
                0 if initial_policy_mapping_inhibit
                else path_length + 1
            ),
            # Steps 1 g-j
            working_public_key=trust_anchor.authority.public_key,
            working_issuer_name=trust_anchor.authority.name,
            # Step 1 k
            max_path_length=max_path_length,
            # NOTE: the algorithm (for now) assumes that the AA CA of RFC 5755
            # is trusted by fiat, and does not require chaining up to a distinct
            # CA. In particular, we assume that the AA CA is the trust anchor in
            # the path. This matches the validation model used in signature
            # policies (where there are separate trust trees for attributes)
            max_aa_path_length=max_aa_path_length
        )
        return state, acceptable_policies

    def update_policy_restrictions(self, cert: x509.Certificate):
        # Step 3 h
        if not cert.self_issued:
            # Step 3 h 1
            if self.explicit_policy != 0:
                self.explicit_policy -= 1
            # Step 3 h 2
            if self.policy_mapping != 0:
                self.policy_mapping -= 1
            # Step 3 h 3
            if self.inhibit_any_policy != 0:
                self.inhibit_any_policy -= 1

        # Step 3 i
        policy_constraints = cert.policy_constraints_value
        if policy_constraints:
            # Step 3 i 1
            require_explicit_policy = \
                policy_constraints['require_explicit_policy'].native
            if require_explicit_policy is not None:
                self.explicit_policy = min(
                    self.explicit_policy, require_explicit_policy
                )
            # Step 3 i 2
            inhibit_policy_mapping = \
                policy_constraints['inhibit_policy_mapping'].native
            if inhibit_policy_mapping is not None:
                self.policy_mapping = min(
                    self.policy_mapping, inhibit_policy_mapping
                )

        # Step 3 j
        if cert.inhibit_any_policy_value is not None:
            self.inhibit_any_policy = min(
                cert.inhibit_any_policy_value.native,
                self.inhibit_any_policy
            )

    def process_policies(self, index: int,
                         certificate_policies, any_policy_uninhibited,
                         proc_state: ValProcState):

        if certificate_policies and self.valid_policy_tree is not None:
            self.valid_policy_tree = update_policy_tree(
                certificate_policies, self.valid_policy_tree,
                depth=index,
                any_policy_uninhibited=any_policy_uninhibited
            )

        # Step 2 e
        elif certificate_policies is None:
            self.valid_policy_tree = None

        # Step 2 f
        if self.valid_policy_tree is None and self.explicit_policy <= 0:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because there is no valid set
                of policies for %s
                ''',
                proc_state.describe_cert()
            ), proc_state)

    def check_name_constraints(self, cert, proc_state: ValProcState):
        # name constraint processing
        whitelist_result = self.permitted_subtrees.accept_cert(cert)
        if not whitelist_result:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because not all names of
                %s are in the permitted namespace of the issuing
                authority. %s
                ''',
                proc_state.describe_cert(),
                whitelist_result.error_message
            ), proc_state)
        blacklist_result = self.excluded_subtrees.accept_cert(cert)
        if not blacklist_result:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because some names of
                %s are excluded from the namespace of the issuing
                authority. %s
                ''',
                proc_state.describe_cert(),
                blacklist_result.error_message
            ), proc_state)

    def check_certificate_signature(self, cert, weak_hash_algos,
                                    proc_state: ValProcState):

        signature_algo = cert['signature_algorithm'].signature_algo
        hash_algo = cert['signature_algorithm'].hash_algo

        if hash_algo in weak_hash_algos:
            raise WeakAlgorithmError.from_state(pretty_message(
                '''
                The path could not be validated because the signature of %s
                uses the weak hash algorithm %s
                ''',
                proc_state.describe_cert(),
                hash_algo
            ), proc_state)

        try:
            validate_sig(
                signature=cert['signature_value'].native,
                signed_data=cert['tbs_certificate'].dump(),
                public_key_info=self.working_public_key,
                sig_algo=signature_algo, hash_algo=hash_algo,
                parameters=cert['signature_algorithm']['parameters']
            )
        except PSSParameterMismatch:
            raise PathValidationError.from_state(pretty_message(
                '''
                The signature parameters for %s do not match the constraints
                on the public key.
                ''',
                proc_state.describe_cert()
            ), proc_state)
        except InvalidSignature:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because the signature of %s
                could not be verified
                ''',
                proc_state.describe_cert()
            ), proc_state)


# TODO allow delegation to calling library here?
SUPPORTED_EXTENSIONS = frozenset([
    'authority_information_access',
    'authority_key_identifier',
    'basic_constraints',
    'crl_distribution_points',
    'extended_key_usage',
    'freshest_crl',
    'key_identifier',
    'key_usage',
    'ocsp_no_check',
    'certificate_policies',
    'policy_mappings',
    'policy_constraints',
    'inhibit_any_policy',
    'name_constraints',
    'subject_alt_name',
    'aa_controls'
])


async def intl_validate_path(validation_context: ValidationContext,
                             path: ValidationPath,
                             proc_state: ValProcState,
                             parameters: PKIXValidationParams = None):
    """
    Internal copy of validate_path() that allows overriding the name of the
    end-entity certificate as used in exception messages. This functionality is
    used during chain validation when dealing with indirect CRLs issuer or
    OCSP responder certificates.

    :param validation_context:
        A pyhanko_certvalidator.context.ValidationContext object to use for
        configuring validation behavior

    :param path:
        A pyhanko_certvalidator.path.ValidationPath object of the path to validate

    :param proc_state:
        Internal state for error reporting and policy application decisions.

    :param parameters:
        Additional input parameters to the PKIX validation algorithm.
        These are not used when validating CRLs and OCSP responses.

    :return:
        The final certificate in the path - an instance of
        asn1crypto.x509.Certificate
    """

    moment = validation_context.moment

    # Inputs

    trust_anchor = path.trust_anchor

    path_length = path.pkix_len

    # Step 1: initialization
    state, acceptable_policies = _PathValidationState\
        .init_pkix_validation_state(path_length, trust_anchor, parameters)

    # Step 2: basic processing
    completed_path: ValidationPath = ValidationPath(
        trust_anchor, interm=[], leaf=None
    )

    cert: Optional[x509.Certificate]
    if isinstance(trust_anchor, CertTrustAnchor):
        # if the trust root has a cert, record it as validated.
        validation_context.record_validation(
            trust_anchor.certificate, completed_path
        )
        cert = trust_anchor.certificate
    else:
        cert = None

    for index in range(1, path_length + 1):
        cert = path[index]

        proc_state.index += 1
        # Step 2 a 1
        state.check_certificate_signature(
            cert, validation_context.weak_hash_algos, proc_state
        )

        # Step 2 a 2
        if not validation_context.is_whitelisted(cert):
            tolerance = validation_context.time_tolerance
            validity = cert['tbs_certificate']['validity']
            _check_validity(
                validity=validity, moment=moment, tolerance=tolerance,
                proc_state=proc_state
            )

        # Step 2 a 3 - CRL/OCSP
        await _check_revocation(
            cert=cert, validation_context=validation_context, path=path,
            proc_state=proc_state
        )

        # Step 2 a 4
        if cert.issuer != state.working_issuer_name:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because %s issuer name
                could not be matched
                ''',
                proc_state.describe_cert(),
            ), proc_state)

        # Steps 2 b-c
        if index == path_length or not cert.self_issued:
            state.check_name_constraints(cert, proc_state=proc_state)

        # Steps 2 d
        state.process_policies(
            index,
            cert.certificate_policies_value,
            #  (see step 2 d 2)
            any_policy_uninhibited=(
                state.inhibit_any_policy > 0 or
                (index < path_length and cert.self_issued)
            ),
            proc_state=proc_state
        )

        if index < path_length:
            # Step 3: prepare for certificate index+1
            _prepare_next_step(index, cert, state, proc_state=proc_state)

        _check_aa_controls(cert, state, index, proc_state=proc_state)

        # Step 3 o / 4 f
        # Check for critical unsupported extensions
        unsupported_critical_extensions = \
            cert.critical_extensions - SUPPORTED_EXTENSIONS
        if unsupported_critical_extensions:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because %s contains the
                following unsupported critical extension%s: %s
                ''',
                proc_state.describe_cert(),
                's' if len(unsupported_critical_extensions) != 1 else '',
                ', '.join(sorted(unsupported_critical_extensions)),
            ), proc_state)

        if validation_context:
            # TODO I left this in from the original code,
            #  but caching intermediate results might not be appropriate at all
            #  times. For example, handling for self-issued certs is different
            #  depending on whether they're treated as an end-entity or not.
            completed_path = completed_path.copy_and_append(cert)
            validation_context.record_validation(cert, completed_path)

    # Step 4: wrap-up procedure

    # Steps 4 c-e skipped since this method doesn't output it
    # Step 4 f skipped since this method defers that to the calling application
    # --> only policy processing remains

    if cert is not None:
        qualified_policies = _finish_policy_processing(
            state=state, cert=cert,
            acceptable_policies=acceptable_policies,
            path_length=path_length,
            proc_state=proc_state
        )
        path._set_qualified_policies(qualified_policies)
        # TODO cache valid policies on intermediate certs too?
        completed_path._set_qualified_policies(qualified_policies)

    return cert


def _check_validity(validity: Validity, moment, tolerance,
                    proc_state: ValProcState):
    if moment < validity['not_before'].native - tolerance:
        raise NotYetValidError.from_state(pretty_message(
            '''
            The path could not be validated because %s is not valid
            until %s
            ''',
            proc_state.describe_cert(),
            validity['not_before'].native.strftime('%Y-%m-%d %H:%M:%SZ')
        ), proc_state)
    if moment > validity['not_after'].native + tolerance:
        raise ExpiredError.from_state(pretty_message(
            '''
            The path could not be validated because %s expired %s
            ''',
            proc_state.describe_cert(),
            validity['not_after'].native.strftime('%Y-%m-%d %H:%M:%SZ')
        ), proc_state)


def _finish_policy_processing(state, cert, acceptable_policies, path_length,
                              proc_state: ValProcState):
    # Step 4 a
    if state.explicit_policy != 0:
        state.explicit_policy -= 1
    # Step 4 b
    if cert.policy_constraints_value:
        if cert.policy_constraints_value['require_explicit_policy'].native == 0:
            state.explicit_policy = 0
    # Step 4 g
    # Step 4 g i
    if state.valid_policy_tree is None:
        intersection = None

    # Step 4 g ii
    elif acceptable_policies == {'any_policy'}:
        intersection = state.valid_policy_tree

    # Step 4 g iii
    else:
        intersection = prune_unacceptable_policies(
            path_length, state.valid_policy_tree, acceptable_policies
        )
    qualified_policies = frozenset()
    if intersection is not None:
        # collect policies in a user-friendly format and attach them to the
        # path object
        def _enum_policies():
            accepted_policy: PolicyTreeNode
            for accepted_policy in intersection.at_depth(path_length):
                listed_pol = accepted_policy.valid_policy
                if listed_pol != 'any_policy':
                    # the first ancestor that is a child of any_policy
                    # will have an ID that makes sense in the user's policy
                    # domain (here 'ancestor' includes the node itself)
                    user_domain_policy_id = next(
                        ancestor.valid_policy
                        for ancestor in accepted_policy.path_to_root()
                        if ancestor.parent.valid_policy == 'any_policy'
                    )
                else:
                    # any_policy can't be mapped, so we don't have to do
                    # any walking up the tree. This also covers the corner case
                    # where the path length is 0 (in this case, PKIX validation
                    # is pointless, but we have to deal with it gracefully)
                    user_domain_policy_id = 'any_policy'

                yield QualifiedPolicy(
                    user_domain_policy_id=user_domain_policy_id,
                    issuer_domain_policy_id=listed_pol,
                    qualifiers=frozenset(accepted_policy.qualifier_set)
                )

        qualified_policies = frozenset(_enum_policies())
    elif state.explicit_policy == 0:
        raise PathValidationError.from_state(pretty_message(
            '''
            The path could not be validated because there is no valid set of
            policies for %s
            ''',
            proc_state.describe_cert()
        ), proc_state)
    return qualified_policies


async def _check_revocation(cert, validation_context: ValidationContext,
                            path: ValidationPath,
                            proc_state: ValProcState):
    ocsp_status_good = False
    revocation_check_failed = False
    ocsp_matched = False
    crl_matched = False
    soft_fail = False
    failures = []
    cert_has_crl, cert_has_ocsp = get_declared_revinfo(cert)
    revinfo_declared = cert_has_crl or cert_has_ocsp
    rev_check_policy = \
        validation_context.revinfo_policy.revocation_checking_policy
    rev_rule = rev_check_policy.ee_certificate_rule if proc_state.is_ee_cert \
        else rev_check_policy.intermediate_ca_cert_rule

    # for OCSP, we don't bother if there's nothing in the certificate's AIA
    if rev_rule.ocsp_relevant and cert_has_ocsp:
        try:
            await verify_ocsp_response(
                cert,
                path,
                validation_context,
                proc_state=proc_state
            )
            ocsp_status_good = True
            ocsp_matched = True
        except OCSPValidationIndeterminateError as e:
            failures.extend([failure[0] for failure in e.failures])
            revocation_check_failed = True
            ocsp_matched = True
        except OCSPNoMatchesError:
            pass
        except OCSPFetchError as e:
            if rev_rule.tolerant:
                soft_fail = True
                validation_context._report_soft_fail(e)
            else:
                failures.append(e)
                revocation_check_failed = True
    if not ocsp_status_good and rev_rule.ocsp_mandatory:
        if failures:
            err_str = '; '.join(str(f) for f in failures)
        else:
            err_str = 'an applicable OCSP response could not be found'
        raise PathValidationError.from_state(pretty_message(
            '''
            The path could not be validated because the mandatory OCSP
            check(s) for %s failed: %s
            ''',
            proc_state.describe_cert(),
            err_str
        ), proc_state)
    status_good = (
        ocsp_status_good and
        rev_rule != RevocationCheckingRule.CRL_AND_OCSP_REQUIRED
    )

    crl_status_good = False
    # do not attempt to check CRLs (even cached ones) if there are no
    # distribution points, unless we have to
    crl_required_by_policy = rev_rule.crl_mandatory or (
        not status_good
        and rev_rule == RevocationCheckingRule.CRL_OR_OCSP_REQUIRED
    )
    crl_fetchable = rev_rule.crl_relevant and cert_has_crl
    if crl_required_by_policy or (crl_fetchable and not status_good):
        try:
            await verify_crl(
                cert, path, validation_context,
                proc_state=proc_state
            )
            revocation_check_failed = False
            crl_status_good = True
            crl_matched = True
        except CRLValidationIndeterminateError as e:
            failures.extend([failure[0] for failure in e.failures])
            revocation_check_failed = True
            crl_matched = True
        except CRLNoMatchesError:
            pass
        except CRLFetchError as e:
            if rev_rule.tolerant:
                soft_fail = True
                validation_context._report_soft_fail(e)
            else:
                failures.append(e)
                revocation_check_failed = True

    if not crl_status_good and rev_rule.crl_mandatory:
        if failures:
            err_str = '; '.join(str(f) for f in failures)
        else:
            err_str = 'an applicable CRL could not be found'
        raise InsufficientRevinfoError.from_state(pretty_message(
            '''
            The path could not be validated because the mandatory CRL
            check(s) for %s failed: %s
            ''',
            proc_state.describe_cert(),
            err_str
        ), proc_state)

    # If we still didn't find a match, the certificate has CRL/OCSP entries
    # but we couldn't query any of them. Let's check if this is disqualifying.
    # With 'strict' the fact that there's no match (irrespective
    # of certificate properties) is enough to cause a failure,
    # otherwise we have to check.
    expected_revinfo = rev_rule.strict or (
        revinfo_declared and
        rev_rule == RevocationCheckingRule.CHECK_IF_DECLARED
    )
    # Did we find any revinfo that "has jurisdiction"?
    matched = crl_matched or ocsp_matched
    expected_revinfo_not_found = not matched and expected_revinfo
    if not soft_fail:
        if not status_good and matched and revocation_check_failed:
            raise InsufficientRevinfoError.from_state(pretty_message(
                '''
                The path could not be validated because %s revocation
                checks failed: %s
                ''',
                proc_state.describe_cert(def_interm=True),
                '; '.join(failures)
            ), proc_state)
        if expected_revinfo_not_found:
            raise InsufficientRevinfoError.from_state(pretty_message(
                '''
                The path could not be validated because no revocation
                information could be found for %s
                ''',
                proc_state.describe_cert()
            ), proc_state)


def _check_aa_controls(cert: x509.Certificate, state: _PathValidationState,
                       index, proc_state: ValProcState):
    aa_controls = AAControls.read_extension_value(cert)
    if aa_controls is not None:
        if not state.aa_controls_used and index > 1:
            raise PathValidationError.from_state(pretty_message(
                '''
                AA controls extension only present on part of the certificate
                chain: %s has AA controls while preceding certificates do not.
                ''',
                proc_state.describe_cert()
            ), proc_state)
        state.aa_controls_used = True
        # deal with path length
        new_max_aa_path_length = aa_controls['path_len_constraint'].native
        if new_max_aa_path_length is not None \
                and new_max_aa_path_length < state.max_aa_path_length:
            state.max_aa_path_length = new_max_aa_path_length
    elif state.aa_controls_used:
        raise PathValidationError.from_state(pretty_message(
            '''
            AA controls extension only present on part of the certificate chain:
            %s has no AA controls
            ''',
            proc_state.describe_cert(),
        ), proc_state)


def _prepare_next_step(index, cert: x509.Certificate,
                       state: _PathValidationState,
                       proc_state: ValProcState):
    if cert.policy_mappings_value:
        policy_map = enumerate_policy_mappings(
            cert.policy_mappings_value,
            proc_state=proc_state
        )

        # Step 3 b
        if state.valid_policy_tree is not None:
            state.valid_policy_tree = apply_policy_mapping(
                policy_map, state.valid_policy_tree, depth=index,
                policy_mapping_uninhibited=state.policy_mapping > 0
            )

    # Step 3 c
    state.working_issuer_name = cert.subject

    # Steps 3 d-f

    # Handle inheritance of DSA parameters from a signing CA to the
    # next in the chain
    # NOTE: we don't perform this step for RSASSA-PSS since there the
    #  parameters are drawn form the signature parameters, where they
    #  must always be present.
    copy_params = None
    if cert.public_key.algorithm == 'dsa' \
            and cert.public_key.hash_algo is None:
        if state.working_public_key.algorithm == 'dsa':
            key_alg = state.working_public_key['algorithm']
            copy_params = key_alg['parameters'].copy()

    if copy_params:
        working_public_key = cert.public_key.copy()
        working_public_key['algorithm']['parameters'] = copy_params
        state.working_public_key = working_public_key
    else:
        state.working_public_key = cert.public_key

    # Step 3 g
    nc_value: x509.NameConstraints = cert.name_constraints_value
    if nc_value is not None:
        new_permitted_subtrees = nc_value['permitted_subtrees']
        if isinstance(new_permitted_subtrees, x509.GeneralSubtrees):
            state.permitted_subtrees.intersect_with(
                process_general_subtrees(new_permitted_subtrees)
            )
        new_excluded_subtrees = nc_value['excluded_subtrees']
        if isinstance(new_excluded_subtrees, x509.GeneralSubtrees):
            state.excluded_subtrees.union_with(
                process_general_subtrees(new_excluded_subtrees)
            )

    # Step 3 h-j
    state.update_policy_restrictions(cert)

    # Step 3 k
    if not cert.ca:
        raise PathValidationError.from_state(pretty_message(
            '''
            The path could not be validated because %s is not a CA
            ''',
            proc_state.describe_cert()
        ), proc_state)

    # Step 3 l
    if not cert.self_issued:
        if state.max_path_length == 0:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because it exceeds the
                maximum path length
                '''
            ), proc_state)
        state.max_path_length -= 1
        if state.max_aa_path_length == 0:
            raise PathValidationError.from_state(pretty_message(
                '''
                The path could not be validated because it exceeds the
                maximum path length for an AA certificate
                '''
            ), proc_state)
        state.max_aa_path_length -= 1

    # Step 3 m
    if cert.max_path_length is not None \
            and cert.max_path_length < state.max_path_length:
        state.max_path_length = cert.max_path_length

    # Step 3 n
    if cert.key_usage_value \
            and 'key_cert_sign' not in cert.key_usage_value.native:
        raise PathValidationError.from_state(pretty_message(
            '''
            The path could not be validated because %s is not allowed
            to sign certificates
            ''',
            proc_state.describe_cert()
        ), proc_state)
