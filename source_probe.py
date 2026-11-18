"""Fixed public endpoints, bounded response bytes, and metadata-only results."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
import json
import re
import resource
import sys
from time import monotonic, process_time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from pypdf import PdfReader

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
NBA = "https://ak-static.cms.nba.com/referee/injury/"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
RELEASES = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/"
HOSTS = {"ak-static.cms.nba.com", "site.api.espn.com", "github.com",
         "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
TABLES = {
    "NBA schedule": ("nba_stats_schedules", "nba_schedule"),
    "NBA team boxes": ("nba_stats_team_boxscores", "team_boxscores"),
    "ESPN schedule": ("espn_nba_schedules", "nba_schedule"),
    "ESPN team boxes": ("espn_nba_team_boxscores", "team_box"),
}
KNOWN = datetime(2026, 11, 16, 10, 15, tzinfo=EASTERN)


def allowed(url):
    origin = urlsplit(url)
    return (origin.scheme == "https" and origin.hostname in HOSTS
            and origin.port in (None, 443) and not origin.username and not origin.password)


class FixedHostRedirects(HTTPRedirectHandler):
    max_repeats = 1
    max_redirections = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise URLError("Unexpected redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(label, url, limit, deadline, *, prefix=False):
    started = monotonic()
    record = dict(source=label, http=None, content_type="", downloaded_bytes=0, check="Unverified")
    body = bytearray()
    headers = {}
    try:
        if not allowed(url) or deadline <= started:
            raise ValueError("Request budget exhausted or invalid endpoint")
        request_headers = {"Cache-Control":"no-cache", "Accept-Encoding":"identity",
                           "User-Agent":"Public-NBA-source-check/1"}
        if prefix:
            request_headers["Range"] = "bytes=0-3"
        request = Request(url, headers=request_headers)
        opener = build_opener(FixedHostRedirects())
        with opener.open(request, timeout=min(4, deadline-started)) as response:
            record.update(http=response.status, content_type=response.headers.get_content_type())
            headers = dict((k.lower(), v) for k, v in response.headers.items())
            if prefix:
                body.extend(response.read(4))
            else:
                while monotonic() < deadline:
                    chunk = response.read1(min(65536, limit+1-len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ValueError("Response exceeds byte budget")
                else:
                    raise ValueError("Run deadline reached while reading")
    except HTTPError as exc:
        record.update(http=exc.code, content_type=exc.headers.get_content_type(), check=f"HTTP {exc.code}")
        exc.close()
        body.clear()
    except (URLError, OSError, ValueError, TimeoutError):
        record["check"] = "Connection, redirect or budget check failed"
        body.clear()
    finally:
        record["downloaded_bytes"] = len(body)
        record["latency_s"] = round(monotonic()-started, 3)
    return bytes(body), headers, record


def report_url(slot):
    return NBA + slot.strftime("Injury-Report_%Y-%m-%d_%I_%M%p.pdf")


def pdf_check(slot, deadline, *, fresh):
    body, _, result = fetch("Official current report" if fresh else "Official known report",
                            report_url(slot), 1_250_000, deadline)
    if not body:
        return result
    try:
        if not body.startswith(b"%PDF-"):
            raise ValueError("PDF signature missing")
        reader = PdfReader(BytesIO(body))
        if not 1 <= len(reader.pages) <= 12:
            raise ValueError("Page budget exceeded")
        header = " ".join((reader.pages[0].extract_text() or "").split())
        match = re.search(r"Injury Report:\s*(\d{2}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})\s*(AM|PM)", header)
        if match is None:
            raise ValueError("Publication header not found")
        published = datetime.strptime(" ".join(match.groups()), "%m/%d/%y %I:%M %p").replace(tzinfo=EASTERN)
        age = (datetime.now(UTC)-published).total_seconds()
        result["publication_utc"] = published.astimezone(UTC).isoformat()
        result["publication_age_s"] = round(age)
        if published != slot or age < 0 or (fresh and age > 3600):
            raise ValueError("Publication slot or freshness check failed")
        result["check"] = "Pass"
    except Exception:
        result["check"] = "PDF structure, publication slot or freshness check failed"
    return result


def espn_check(day, deadline):
    body, headers, result = fetch("ESPN current scoreboard", ESPN+f"?dates={day:%Y%m%d}&limit=100",
                                  2_500_000, deadline)
    if not body:
        return result
    try:
        server = parsedate_to_datetime(headers["date"]) + timedelta(seconds=int(headers.get("age","0")))
        age = abs((datetime.now(UTC)-server).total_seconds())
        events = json.loads(body)["events"]
        if age > 120 or not isinstance(events,list):
            raise ValueError("Freshness or JSON schema check failed")
        if any(datetime.fromisoformat(e["date"]).astimezone(EASTERN).date()!=day for e in events):
            raise ValueError("Unexpected game date")
        result.update(check="Pass", games=len(events), http_clock_offset_s=round(age))
    except (KeyError, TypeError, ValueError, OverflowError):
        result["check"] = "Scoreboard date, shape or HTTP clock check failed"
    return result


def history_check(task, deadline):
    name, year, url = task
    body, _, result = fetch(f"{name} {year}", url, 4, deadline, prefix=True)
    if body:
        result["check"] = "Pass" if body==b"PAR1" and result["http"] in (200,206) else "Parquet signature missing"
    return result


def run_probe():
    begin, cpu = monotonic(), process_time()
    deadline = begin+45
    now = datetime.now(EASTERN)
    recent = now.replace(minute=now.minute//15*15, second=0, microsecond=0)
    known = pdf_check(KNOWN, deadline, fresh=False)
    reports = []
    for offset in range(3):
        reports.append(pdf_check(recent-timedelta(minutes=15*offset), deadline, fresh=True))
        if reports[-1]["check"]=="Pass":
            break
    espn = espn_check(now.date(), deadline)
    tasks = [(name, year, RELEASES+f"{tag}/{filename}_{year}.parquet")
             for year in range(2027,2013,-1) for name,(tag,filename) in TABLES.items()]
    with ThreadPoolExecutor(max_workers=4) as executor:
        history = list(executor.map(lambda task: history_check(task, deadline), tasks))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return dict(observed_utc=datetime.now(UTC).isoformat(), wall_s=round(monotonic()-begin,3),
                cpu_s=round(process_time()-cpu,3), process_peak_mib=round(rss/(1048576 if sys.platform=="darwin" else 1024),2),
                checks={"Known official PDF":known["check"]=="Pass",
                        "Current official PDF":any(r["check"]=="Pass" for r in reports),
                        "Current ESPN scoreboard":espn["check"]=="Pass",
                        "56 public Parquet prefixes (2014–2027)":all(r["check"]=="Pass" for r in history)},
                requests=[known,*reports,espn,*history])
