"""Web push: "good flying tomorrow" the evening before, at most once a day per device.

VAPID keys are made on first start and kept in the data folder.
"""
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

log = logging.getLogger("spotter.push")
CONTACT = "mailto:spotter@tamairnv.co.uk"


class Push:
    def __init__(self, data_dir):
        self.pem = data_dir / "vapid.pem"
        if not self.pem.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            self.pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            self.pem.chmod(0o600)
        self.vapid = Vapid01.from_file(str(self.pem))
        raw = self.vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def send(self, sub: dict, payload: dict) -> bool:
        """False when the subscription is gone (unsubscribed, app removed) and should be dropped."""
        try:
            webpush(sub, json.dumps(payload), vapid_private_key=str(self.pem), vapid_claims={"sub": CONTACT}, ttl=6 * 3600)
            return True
        except WebPushException as e:
            code = e.response.status_code if e.response is not None else None
            if code in (404, 410):
                return False
            log.warning("push failed: %s", e)
            return True
