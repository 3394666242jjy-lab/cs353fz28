"""Run a synthetic end-to-end M1 check against an already running real service.

Requirements: pypdf and httpx. Set M1_USER_EMAIL/M1_USER_PASSWORD and
M1_OTHER_EMAIL/M1_OTHER_PASSWORD or enter credentials interactively. Two distinct
seed accounts are required. This creates persistent synthetic documents, a job,
and an analysis; it does not delete them. Never use personal/production accounts.
The server must use real configured embedding/generation services, MongoDB,
Qdrant, Redis and a running Celery worker; this script does not provide substitutes.

Example: python scripts/m1_acceptance.py --base-url http://127.0.0.1:8000 --timeout 600
Local input generation check only: python scripts/m1_acceptance.py --self-test
"""

import argparse
import getpass
import json
import os
import sys
import time
from io import BytesIO
from uuid import uuid4

import httpx
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

PDF_PAGES = [
    "Synthetic candidate built Python services and designed MongoDB schemas.",
    "",
    "Implemented Redis cache for a backend service; no benchmark was recorded.",
]
PROJECT_TEXT = (
    "Synthetic project description\n"
    "Designed MongoDB collections, fields and indexes.\n"
    "Implemented Redis cache; no latency measurements were recorded.\n"
)
JD = (
    "Backend developer internship. Required: design MongoDB data models. "
    "Required: measurable backend performance improvements using Redis. "
    "Preferred: build and deploy Docker images."
)


def make_pdf():
    writer = PdfWriter()
    for text in PDF_PAGES:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def self_test():
    pages = PdfReader(BytesIO(make_pdf())).pages
    assert len(pages) == 3
    assert [page.extract_text().strip() for page in pages] == PDF_PAGES
    assert PROJECT_TEXT.splitlines()[1].startswith("Designed MongoDB")
    print("PASS: synthetic PDF has three original pages including the blank page; TXT is UTF-8.")


def credential(env_name, label, *, password=False):
    value = os.environ.get(env_name)
    if value:
        return value
    if not sys.stdin.isatty():
        raise RuntimeError(f"Missing {env_name}; provide it via environment or interactive input")
    return getpass.getpass(label + ": ") if password else input(label + ": ").strip()


class Audit:
    def __init__(self, base_url, timeout):
        self.client = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=30)
        self.deadline_seconds = timeout
        self.findings = []

    def check(self, name, ok, detail=""):
        self.findings.append({"check": name, "pass": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))

    def request(self, method, path, *, token=None, expected=200, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code != expected:
            try:
                error_code = response.json().get("error", {}).get("code", "unknown")
            except (ValueError, AttributeError):
                error_code = "unknown"
            raise RuntimeError(
                f"{method} {path.split('?')[0]} returned HTTP {response.status_code} "
                f"({error_code}), expected {expected}"
            )
        try:
            return response.json().get("data")
        except ValueError as exc:
            raise RuntimeError(f"{method} {path} did not return JSON") from exc

    def login(self, email, password):
        result = self.request("POST", "/auth/login", json={"email": email, "password": password})
        token = result["access_token"]
        user = self.request("GET", "/me", token=token)
        return token, user["id"]

    def expect_denied(self, label, token, path, *, params=None):
        response = self.client.get(
            path, headers={"Authorization": f"Bearer {token}"}, params=params
        )
        self.check(
            label,
            response.status_code == 404,
            f"HTTP {response.status_code}; expected 404",
        )

    def wait_execution(self, token, execution_id):
        until = time.monotonic() + self.deadline_seconds
        last = None
        while time.monotonic() < until:
            result = self.request("GET", f"/executions/{execution_id}", token=token)
            if result["status"] != last:
                last = result["status"]
                print(f"  execution {execution_id}: {last}")
            if last == "succeeded":
                return result
            if last in {"failed", "waiting_for_input"}:
                code = (result.get("error") or {}).get("code", "none")
                raise RuntimeError(f"Execution {execution_id} ended {last} (code {code})")
            time.sleep(2)
        raise RuntimeError(
            f"Execution {execution_id} did not finish within {self.deadline_seconds}s; last={last}"
        )

    def upload(self, token, name, data, kind, mime):
        result = self.request(
            "POST",
            "/documents",
            token=token,
            expected=202,
            files={"file": (name, data, mime)},
            data={"kind": kind},
        )
        return result["document_id"], result["execution_id"]

    def run(self, user_email, user_password, other_email, other_password):
        print(
            "This check creates synthetic documents, a job and an analysis "
            "in the first account; it does not delete them."
        )
        token, user_id = self.login(user_email, user_password)
        other, other_id = self.login(other_email, other_password)
        if user_id == other_id:
            raise RuntimeError("The two test accounts must be distinct")
        self.check("two independent accounts", True)
        run_id = uuid4().hex[:8]
        bad = self.client.post(
            "/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
            data={"kind": "resume"},
        )
        self.check(
            "disguised PDF rejected before persistence",
            bad.status_code in (415, 422),
            f"HTTP {bad.status_code}",
        )
        bad = self.client.post(
            "/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("fake.txt", make_pdf(), "text/plain")},
            data={"kind": "resume"},
        )
        self.check(
            "PDF disguised as TXT rejected before persistence",
            bad.status_code in (415, 422),
            f"HTTP {bad.status_code}",
        )
        pdf_id, pdf_exec = self.upload(
            token, f"m1-{run_id}.pdf", make_pdf(), "resume", "application/pdf"
        )
        txt_id, txt_exec = self.upload(
            token,
            f"m1-{run_id}.txt",
            PROJECT_TEXT.encode("utf-8"),
            "project",
            "text/plain",
        )
        self.expect_denied("other account cannot read document", other, f"/documents/{pdf_id}")
        self.expect_denied("other account cannot read execution", other, f"/executions/{pdf_exec}")
        for doc_id, execution_id in [(pdf_id, pdf_exec), (txt_id, txt_exec)]:
            self.wait_execution(token, execution_id)
            doc = self.request("GET", f"/documents/{doc_id}", token=token)
            self.check(
                f"document ready: {doc_id}",
                doc["status"] == "ready",
                f"status={doc['status']}",
            )
        job = self.request(
            "POST",
            "/jobs",
            token=token,
            expected=201,
            json={"title": "Synthetic backend internship", "raw_jd": JD},
        )
        job_id = job["job_id"]
        self.expect_denied("other account cannot read job", other, f"/jobs/{job_id}")
        execution_id = self.request(
            "POST",
            "/analyses",
            token=token,
            expected=202,
            json={"job_id": job_id, "document_ids": [pdf_id, txt_id]},
        )["execution_id"]
        self.expect_denied(
            "other account cannot read analysis execution",
            other,
            f"/executions/{execution_id}",
        )
        done = self.wait_execution(token, execution_id)
        analysis_id = done["result_id"]
        self.expect_denied("other account cannot read analysis", other, f"/analyses/{analysis_id}")
        analysis = self.request("GET", f"/analyses/{analysis_id}", token=token)
        print("Actual model assessments (manual review required):")
        for match in analysis["matches"]:
            print(
                json.dumps(
                    {
                        k: match[k]
                        for k in (
                            "requirement",
                            "status",
                            "explanation",
                            "missing_information",
                        )
                    },
                    ensure_ascii=False,
                )
            )
        found_pdf = found_text = False
        refs = [ref for match in analysis["matches"] for ref in match["evidence_refs"]]
        self.check("analysis contains citations", bool(refs), f"count={len(refs)}")
        seen = set()
        for ref in refs:
            key = (ref["chunk_id"], ref["version"])
            if key in seen:
                continue
            seen.add(key)
            params = {"version": ref["version"]}
            evidence = self.request("GET", f"/evidence/{key[0]}", token=token, params=params)
            self.expect_denied(
                "other account cannot read evidence",
                other,
                f"/evidence/{key[0]}",
                params=params,
            )
            locator, quote = evidence["locator"], evidence["quote"]
            if evidence["document_id"] == pdf_id and locator["kind"] == "pdf":
                page = locator["page"]
                valid = 1 <= page <= len(PDF_PAGES) and quote in PDF_PAGES[page - 1]
                self.check(f"PDF citation page {page}", valid)
                found_pdf |= valid and page == 3
            elif evidence["document_id"] == txt_id and locator["kind"] == "text":
                start, end = locator["line_start"], locator["line_end"]
                lines = PROJECT_TEXT.splitlines()
                valid = 1 <= start <= end <= len(lines) and quote == "\n".join(
                    lines[start - 1 : end]
                )
                self.check(f"TXT citation lines {start}-{end}", valid)
                found_text |= valid
            else:
                self.check("citation belongs to uploaded synthetic documents", False)
        self.check(
            "model returned a verifiable PDF citation on page 3 after the blank page",
            found_pdf,
        )
        self.check("model returned a verifiable TXT citation", found_text)
        print(
            "IDs:",
            json.dumps(
                {
                    "pdf": pdf_id,
                    "txt": txt_id,
                    "job": job_id,
                    "analysis": analysis_id,
                    "analysis_execution": execution_id,
                }
            ),
        )
        print(
            "Interpretation: model relevance, all three categories and lack of invented "
            "Docker experience require manual review."
        )
        return all(item["pass"] for item in self.findings)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum seconds per execution")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate generated inputs only; no network",
    )
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    try:
        audit = Audit(args.base_url, args.timeout)
        ok = audit.run(
            credential("M1_USER_EMAIL", "First test email"),
            credential("M1_USER_PASSWORD", "First test password", password=True),
            credential("M1_OTHER_EMAIL", "Second test email"),
            credential("M1_OTHER_PASSWORD", "Second test password", password=True),
        )
        return 0 if ok else 1
    except (RuntimeError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
