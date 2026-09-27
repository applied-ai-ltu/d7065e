"""Push the D7065E dashboards to Grafana.

Credentials (grafana_url, grafana_user, grafana_password) come from Vault (homelab/d7065e)
via fds.secrets; the TimescaleDB datasource is resolved by name so the JSON stays portable.
Dashboards land in the "D7065E" folder, shared with team applied-ai-d7065e.

    python scripts/grafana_provision.py            # build + push both
    python scripts/grafana_provision.py --no-build # push the JSON as-is
"""

import json
import subprocess
import sys
from pathlib import Path

import requests
import urllib3

from fds.secrets import get_secrets

ROOT = Path(__file__).resolve().parents[1]
GRAFANA_DIR = ROOT / "grafana"
DATASOURCE_NAME = "D7065E-TimescaleDB"
FOLDER = {"uid": "d7065e", "title": "D7065E"}
TEAM = "applied-ai-d7065e"
DASHBOARDS = ["operations.json", "technical.json"]

urllib3.disable_warnings()


class Grafana:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.verify = False

    def get(self, path, **kw):
        r = self.s.get(self.url + path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, payload):
        r = self.s.post(self.url + path, json=payload)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # -- pieces
    def datasource_uid(self, name):
        return self.get(f"/api/datasources/name/{name}")["uid"]

    def ensure_folder(self):
        r = self.s.get(f"{self.url}/api/folders/{FOLDER['uid']}")
        if r.status_code == 200:
            return "exists"
        self.post("/api/folders", FOLDER)
        print(f"created folder {FOLDER['title']}")
        return "created"

    def share_folder_with_team(self, team_name):
        teams = self.get("/api/teams/search", params={"name": team_name}).get("teams", [])
        if not teams:
            print(f"team {team_name!r} not found; folder permissions left unchanged")
            return
        r = self.s.post(
            f"{self.url}/api/folders/{FOLDER['uid']}/permissions",
            json={"items": [{"teamId": teams[0]["id"], "permission": 2}]},  # 2 = Edit
        )
        print(f"folder shared with team {team_name}: {'ok' if r.ok else r.text[:120]}")

    def push(self, dashboard):
        res = self.post(
            "/api/dashboards/db",
            {
                "dashboard": dashboard,
                "folderUid": FOLDER["uid"],
                "overwrite": True,
                "message": "provisioned by scripts/grafana_provision.py",
            },
        )
        return f"{self.url}{res['url']}"


def main():
    if "--no-build" not in sys.argv:
        subprocess.run([sys.executable, str(GRAFANA_DIR / "build_dashboards.py")], check=True)

    s = get_secrets()
    g = Grafana(s["grafana_url"], s["grafana_user"], s["grafana_password"])
    ds_uid = g.datasource_uid(DATASOURCE_NAME)
    print(f"datasource {DATASOURCE_NAME} -> {ds_uid}")
    if g.ensure_folder() == "created":
        g.share_folder_with_team(TEAM)

    for name in DASHBOARDS:
        raw = (GRAFANA_DIR / name).read_text().replace('"DS_TIMESCALE"', json.dumps(ds_uid))
        dash = json.loads(raw)
        dash.pop("id", None)
        print(f"{dash['title']:45s} -> {g.push(dash)}")


if __name__ == "__main__":
    main()
