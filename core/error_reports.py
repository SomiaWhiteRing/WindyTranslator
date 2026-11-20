"""Private, bounded error reports. The sending thread never reports its own errors."""

import json
import logging
import platform
import random
import re
import sqlite3
import threading
import time
import traceback
import uuid
from contextlib import closing
from contextvars import ContextVar
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from core import updates

log = logging.getLogger(__name__)
MAX_BYTES = 500_000
MAX_PENDING = 32
failed_tool = ContextVar("windy_failed_tool", default=None)


def validation_kind(reason):
    if "假名" in reason:
        return "residual-kana"
    if any(label in reason for label in ("行首", "行尾", "开头", "结尾", "移动到了其他行")):
        return "control-position"
    if "控制" in reason or "标记" in reason:
        return "control-token"
    if "行数" in reason:
        return "line-count"
    if "JSON" in reason or "缺少该条" in reason:
        return "invalid-response"
    return "validation"


def issue_signature(kind, evidence):
    entry = evidence.get("failure", {})
    reason = entry.get("reason") or evidence.get("reason") or evidence.get("message") or evidence.get("exception") or kind
    rule = re.split(r"。|；|请按配置|原文[=：(]|译文[=：]", reason, maxsplit=1)[0]
    frames = re.findall(r'File "([^"\n]+)", line \d+, in ([^\n]+)', evidence.get("stack") or "")
    location = (re.split(r"[/\\]", frames[-1][0])[-1], frames[-1][1].strip()) if frames else ()
    return (kind, evidence.get("task"), entry.get("type"), entry.get("stage"), evidence.get("exception"),
            location, re.sub(r"\d+", "#", rule)[:180])


class ErrorReports:
    def __init__(self, directory, build, config):
        self.db = Path(directory) / "windy-errors.sqlite"
        self.site = updates.normalize_site_url(build.get("updateSite", updates.DEFAULT_SITE_URL))
        self.build = build
        self.config = config
        self.enabled = config.get("report_error_logs", True)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._send_loop, name="windy-error-reports", daemon=True)
        self.closed = False
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY, bucket TEXT, kind TEXT, version TEXT, build TEXT,
                evidence TEXT, n INTEGER, acknowledged INTEGER DEFAULT 0,
                next_sample REAL DEFAULT 0, window_end REAL, failures INTEGER DEFAULT 0
              );
              CREATE TABLE IF NOT EXISTS schedule (site TEXT PRIMARY KEY, due REAL);
            """)
            if not self.enabled:
                db.execute("DELETE FROM reports")
                db.commit()
        self.thread.start()

    def set_enabled(self, enabled):
        with self.lock:
            self.enabled = enabled
            if not enabled:
                try:
                    with closing(sqlite3.connect(self.db)) as db, db:
                        db.execute("DELETE FROM reports")
                except sqlite3.Error:
                    log.warning("清理错误报告队列失败")
        self.wake.set()

    def close(self):
        self.closed = True
        self.wake.set()

    def exception(self, kind, task, exc):
        self.capture(
            kind,
            task,
            {
                "task": task,
                "exception": type(exc).__name__,
                "stack": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            },
        )

    def capture(self, kind, task, evidence, sensitive_values=()):
        if not self.enabled:
            return
        try:
            self._save(kind, task, evidence, sensitive_values)
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            log.warning("无法保存错误报告", exc_info=False)

    def _save(self, kind, task, evidence, sensitive_values):
        with self.lock, closing(sqlite3.connect(self.db)) as db, db:
            if not self.enabled:
                return
            # A new time window retains a fresh sample even for a long-running task.
            now = time.time()
            # Different failures in the same broad category need distinct evidence.
            fingerprint = uuid.uuid5(uuid.NAMESPACE_OID, json.dumps(issue_signature(kind, evidence))).hex
            bucket = f"{task}:{fingerprint}:{int(now // 600)}"
            db.execute("DELETE FROM reports WHERE window_end<=? AND n=acknowledged", (now,))
            updated = db.execute("UPDATE reports SET n=min(1000000,n+1) WHERE bucket=?", (bucket,)).rowcount
            if updated or db.execute("SELECT count(*) FROM reports").fetchone()[0] >= MAX_PENDING:
                return
            secrets = {
                str(block[key])
                for block in self.config.values()
                if isinstance(block, dict)
                for key in ("api_key", "api_url")
                if isinstance(block.get(key), str) and block[key]
            }
            secrets.update(v for v in sensitive_values if isinstance(v, str) and v)
            cuts = []

            def clean(value, field):
                if isinstance(value, dict):
                    return {
                        k: clean(v, k)
                        for k, v in value.items()
                        if k.lower()
                        not in (
                            "authorization",
                            "proxy-authorization",
                            "api_key",
                            "cookie",
                            "set-cookie",
                            "password",
                            "token",
                            "api_url",
                            "x-api-key",
                        )
                    }
                if isinstance(value, (list, tuple)):
                    return [clean(v, field) for v in value]
                if not isinstance(value, str):
                    return value
                for secret in secrets:
                    value = value.replace(secret, "[redacted]")
                value = re.sub(
                    r"(?im)^([ \t]*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key):)[^\n\r]*",
                    r"\1 [redacted]",
                    value,
                )
                value = re.sub(r"(?i)(?:[A-Z]:[/\\]Users[/\\]|/Users/|/home/)[^/\\\s\"']+", "<home>", value)
                value = re.sub(
                    r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.-]+|\bsk-[\w-]{8,}", "[redacted]", value
                )
                value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", value)
                value = re.sub(
                    r"(?i)([?&](?:key|token|access_token|api_key|signature)=)[^&\s\"']+",
                    r"\1[redacted]",
                    value,
                )
                value = re.sub(
                    r"(?i)([\"']?(?:api[_-]?key|password|authorization|cookie|access_token)[\"']?\s*[:=]\s*)([\"'][^\"'\n]*[\"']|[^\s,;]+)",
                    r"\1[redacted]",
                    value,
                )
                if len(value) > 48000:
                    cuts.append({"field": field, "originalChars": len(value)})
                    return value[:48000]
                return value

            evidence = dict(evidence, windows=platform.platform(), capturedAt=now)
            if evidence.get("tool"):
                # The release manifest identifies the exact bundled tool even without a version resource.
                manifest = self.db.parent / "package-files.json"
                if manifest.is_file():
                    try:
                        files = json.loads(manifest.read_bytes())["files"]
                        evidence["tool"] = dict(
                            evidence["tool"],
                            packageFile=next(
                                (
                                    v
                                    for k, v in files.items()
                                    if k.lower().endswith("/rpgrewriter/rpgrewriter.exe")
                                ),
                                None,
                            ),
                        )
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
            evidence = clean(evidence, "")
            evidence["truncated"] = cuts

            def packed():
                return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))

            encoded = packed()
            attempts = evidence.get("attempts", [])
            while len(encoded.encode()) > MAX_BYTES and len(attempts) > 1:
                cuts.append({"omittedAttempt": attempts.pop(0).get("request")})
                encoded = packed()
            if len(encoded.encode()) > MAX_BYTES:
                # Keep the structural comparisons. Mark over-budget bulk fields explicitly.
                for attempt in attempts:
                    for field in ("messages", "response"):
                        if field in attempt:
                            cuts.append({"field": field, "reason": "report size"})
                            del attempt[field]
                encoded = packed()
            if len(encoded.encode()) > MAX_BYTES:
                evidence = {
                    "task": task,
                    "capturedAt": now,
                    "truncated": [{"reason": "report size", "originalBytes": len(encoded.encode())}],
                    "preview": encoded[:50000],
                }
                encoded = packed()
            db.execute(
                "INSERT INTO reports(id,bucket,kind,version,build,evidence,n,window_end) VALUES(?,?,?,?,?,?,1,?)",
                (
                    uuid.uuid4().hex,
                    bucket,
                    kind,
                    self.build["version"],
                    self.build.get("applicationBuildId", ""),
                    encoded,
                    (int(now // 600) + 1) * 600,
                ),
            )
        self.wake.set()

    def _send_loop(self):
        while not self.closed:
            self.wake.clear()
            try:
                delay = self._send_one() if self.enabled else 3600
            except (OSError, sqlite3.Error, ValueError, HTTPException, updates.UpdateError):
                log.warning("错误报告发送暂缓")
                delay = 60
            self.wake.wait(delay)

    def _send_one(self):
        with self.lock, closing(sqlite3.connect(self.db)) as db, db:
            if not self.enabled or self.closed:
                return 3600
            now = time.time()
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM reports WHERE window_end<=? AND n=acknowledged", (now,))
            due = db.execute("SELECT due FROM schedule WHERE site=?", (self.site,)).fetchone()
            if due and due[0] > now:
                return min(due[0] - now, 3600)
            row = db.execute(
                "SELECT id,kind,version,build,evidence,n,failures FROM reports WHERE n>acknowledged AND next_sample<=? ORDER BY rowid LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return 60
            identifier, kind, version, build, evidence, n, failures = row
            delay = min(3600, 30 * 2 ** min(7, failures)) + random.uniform(1, 10)
            db.execute("UPDATE reports SET failures=failures+1 WHERE id=?", (identifier,))
            db.execute("INSERT OR REPLACE INTO schedule VALUES(?,?)", (self.site, now + delay))
        payload = {
            "schema": 1, "id": identifier, "kind": kind, "version": version, "build": build,
            "evidence": json.loads(evidence), "occurrences": n,
        }
        req = Request(
            self.site + "/api/tools/windy-translator/error-reports",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "WindyTranslator-Reports/1"},
        )
        if not self.enabled or self.closed:
            return delay
        try:
            with build_opener(updates._SiteRedirectHandler(self.site)).open(req, timeout=12) as response:
                accepted = response.status == 204
        except HTTPError as err:
            if err.code in (429, 503):
                retry = err.headers.get("Retry-After", "")
                if retry.isdigit():
                    delay = max(delay, min(86400, int(retry)))
            if err.code in (400, 413, 415, 422):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute("DELETE FROM reports WHERE id=?", (identifier,))
            accepted = False
        except (URLError, OSError):
            accepted = False
        with self.lock, closing(sqlite3.connect(self.db)) as db, db:
            if accepted:
                db.execute(
                    "UPDATE reports SET acknowledged=max(acknowledged,?),evidence='{}',failures=0,next_sample=window_end WHERE id=?",
                    (n, identifier),
                )
                delay = 15
            db.execute("INSERT OR REPLACE INTO schedule VALUES(?,?)", (self.site, time.time() + delay))
        return delay
