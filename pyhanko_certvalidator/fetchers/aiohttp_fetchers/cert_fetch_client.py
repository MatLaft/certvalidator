from typing import Union, Iterable

import aiohttp
import logging
from asn1crypto import x509, cms

from ...errors import CertificateFetchError
from ..api import CertificateFetcher
from .util import AIOHttpMixin, LazySession
from ..common_utils import (
    unpack_cert_content, complete_certificate_fetch_jobs,
    ACCEPTABLE_STRICT_CERT_CONTENT_TYPES,
    ACCEPTABLE_CERT_PEM_ALIASES, gather_aia_issuer_urls
)


logger = logging.getLogger(__name__)


class AIOHttpCertificateFetcher(CertificateFetcher, AIOHttpMixin):
    def __init__(self, session: Union[aiohttp.ClientSession, LazySession],
                 user_agent=None, per_request_timeout=10, permit_pem=True):
        super().__init__(session, user_agent, per_request_timeout)
        self.permit_pem = permit_pem

    async def fetch_certs(self, url, url_origin_type):
        """
        Fetch one or more certificates from a URL.

        :param url:
            URL to fetch.
        :param url_origin_type:
            Parameter indicating where the URL came from (e.g. 'CRL'),
            for error reporting purposes.
        :raises:
            CertificateFetchError - when a network I/O or decoding error occurs
        :return:
            An iterable of asn1crypto.x509.Certificate objects.
        """

        async def task():
            try:
                logger.info(f"Fetching certificates from {url}...")
                return await _grab_certs(
                    url, permit_pem=self.permit_pem,
                    timeout=self.per_request_timeout,
                    user_agent=self.user_agent,
                    session=await self.get_session(),
                    url_origin_type=url_origin_type
                )
            except (ValueError, aiohttp.ClientError) as e:
                msg = f"Failed to fetch certificate(s) from url {url}."
                logger.debug(msg, exc_info=e)
                raise CertificateFetchError(msg)

        return await self._post_fetch_task(url, task)

    def fetch_cert_issuers(
            self, cert: Union[x509.Certificate, cms.AttributeCertificateV2]):

        fetch_jobs = [
            self.fetch_certs(url, url_origin_type='certificate')
            for url in gather_aia_issuer_urls(cert)
        ]

        if isinstance(cert, x509.Certificate):
            target = cert.subject.human_friendly
        else:
            # TODO log audit ID
            target = "attribute certificate"
        logger.info(f"Retrieving issuer certs for {target}...")
        return complete_certificate_fetch_jobs(fetch_jobs)

    def fetch_crl_issuers(self, certificate_list):
        fetch_jobs = [
            self.fetch_certs(url, url_origin_type='CRL')
            for url in certificate_list.issuer_cert_urls
        ]
        return complete_certificate_fetch_jobs(fetch_jobs)

    def fetched_certs(self) -> Iterable[x509.Certificate]:
        return self.get_results()


async def _grab_certs(url, *, user_agent, session: aiohttp.ClientSession,
                      url_origin_type, timeout, permit_pem=True):
    """
    Grab one or more certificates from a caIssuers URL.

    We accept two types of content in the response:
      - A single DER-encoded X.509 certificate
      - A PKCS#7 'certs-only' SignedData message
      - PEM-encoded certificates (if permit_pem=True)

    Note: strictly speaking, you're not supposed to use PEM to serve certs for
    AIA purposes in PEM format, but people do it anyway.
    """

    acceptable_cts = ACCEPTABLE_STRICT_CERT_CONTENT_TYPES
    if permit_pem:
        acceptable_cts += ACCEPTABLE_CERT_PEM_ALIASES

    headers = {
        'Accept': ','.join(acceptable_cts),
        'User-Agent': user_agent
    }
    cl_timeout = aiohttp.ClientTimeout(timeout)
    async with session.get(url=url, headers=headers, timeout=cl_timeout,
                           raise_for_status=True) as response:
        response_data = await response.read()
        ct_err = None
        try:
            content_type = response.headers['Content-Type'].strip()
            if content_type not in acceptable_cts:
                ct_err = (
                    f"Unacceptable content type '{repr(content_type)}' "
                    f"when fetching issuer certificate for {url_origin_type} "
                    f"from URL {url}."
                )
        except KeyError:
            ct_err = (
                f"Unclear content type when fetching issuer "
                f"certificate for {url_origin_type} from URL "
                f"{url}."
            )

        if ct_err is not None:
            raise aiohttp.ContentTypeError(
                response.request_info, response.history,
                message=ct_err, headers=response.headers,
            )
    certs = unpack_cert_content(response_data, content_type, url, permit_pem)
    return list(certs)
