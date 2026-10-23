"""One verified community-account snapshot shared by every step of a bot run."""
import argparse
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HANDLES = ("ToBulaer", "ToBuerma", "KawasawaSen", "BulmaList")
DEFAULT_ORIGIN = "https://daily-links.guerridodominique142615.workers.dev"
POLICY_PATH_ENV = "COMMUNITY_MENTIONS_FILE"
SYNC_PATH = "/api/sync/community-mentions"
MIN_REQUIRED_MENTIONS = 1


def validate_policy(value):
    if not isinstance(value, dict):
        raise ValueError("社区账号设置格式无效")
    handles = value.get("handles")
    if not isinstance(handles, list) or not 1 <= len(handles) <= 20:
        raise ValueError("社区账号必须有1–20个")
    normalized = []
    for handle in handles:
        if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise ValueError("社区X用户名无效")
        if handle.lower() in normalized:
            raise ValueError("社区账号不能重复")
        normalized.append(handle.lower())
    revision = value.get("revision")
    # Accept the previous wire format during rollout; matching always uses the new any-one rule.
    if type(revision) is not int or revision < 0 or type(value.get("minimum")) is not int or value["minimum"] not in (1, 2):
        raise ValueError("社区账号版本或匹配规则无效")
    return {"handles": normalized, "minimum": MIN_REQUIRED_MENTIONS, "revision": revision,
            "updatedAt": value.get("updatedAt")}


def load_target_mentions():
    path = os.getenv(POLICY_PATH_ENV, "")
    if not path:
        # Offline tests and legacy standalone callers retain the original policy.
        return DEFAULT_HANDLES
    policy = validate_policy(json.loads(Path(path).read_text(encoding="utf-8")))
    return tuple(policy["handles"])


def fetch_policy(secret, origin=DEFAULT_ORIGIN, post=None, timestamp=None, request_id=None):
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("未配置网站签名密钥，停止本轮收录与回复")
    origin = origin.rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("网站设置来源必须是HTTPS根地址")
    if post is None:
        import requests
        post = requests.post
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    request_id = request_id or "community-" + uuid.uuid4().hex
    body = b"{}"
    canonical = "\n".join(("POST", SYNC_PATH, stamp, request_id, hashlib.sha256(body).hexdigest()))
    headers = {"Content-Type": "application/json", "X-Sync-Timestamp": stamp,
               "X-Sync-Request-Id": request_id,
               "X-Sync-Signature": hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()}
    response = post(origin + SYNC_PATH, data=body, headers=headers, timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError(f"读取社区账号设置失败（HTTP {response.status_code}），停止本轮收录与回复")
    return validate_policy(response.json())


def save_policy(path, policy):
    policy = validate_policy(policy)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(policy, stream, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        policy = fetch_policy(os.getenv("WEBSITE_SYNC_SECRET", ""), os.getenv("WEBSITE_SYNC_URL") or DEFAULT_ORIGIN)
        save_policy(args.output, policy)
    except Exception:
        # Do not echo request exceptions: they may contain headers or credentials.
        print("社区账号设置同步失败：本轮不收录、不发回复；不会退回旧账号规则。")
        return 1
    print(f"社区账号设置已读取：版本{policy['revision']}，{len(policy['handles'])}个账号，主推帖匹配任意1个即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
