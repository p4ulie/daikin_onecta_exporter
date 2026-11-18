import json
import re
import subprocess

import yaml

CHART = "helm/daikin-onecta-exporter"
REQUIRED = ["--set", "onecta.secretName=daikin-onecta"]
TOGGLES = ["--set", "serviceMonitor.enabled=true", "--set", "prometheusRule.enabled=true", "--set", "dashboard.enabled=true"]
# Fixed names from exporter.py (the counter gains _total on export).
KNOWN = {
    "daikin_up", "daikin_login_ok", "daikin_last_success_timestamp_seconds", "daikin_read_duration_seconds",
    "daikin_read_errors_total", "daikin_authorized_timestamp_seconds", "daikin_ratelimit_remaining_day",
    "daikin_ratelimit_remaining_minute", "daikin_poll_interval_seconds", "daikin_token_saved",
    "daikin_temperature_celsius", "daikin_setpoint_celsius", "daikin_on", "daikin_operation_mode_info",
    "daikin_powerful_mode_active", "daikin_error_state", "daikin_warning_state", "daikin_emergency_state",
    "daikin_error_code_info", "daikin_energy_today_kwh", "daikin_energy_yesterday_kwh", "daikin_info",
}


def template(*args):
    return subprocess.run(["helm", "template", "t", CHART, *args], capture_output=True, text=True)


def render_all(*args):
    out = template(*args)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render(*args):
    return {d["kind"]: d for d in render_all(*args)}


def test_requires_secret_name():
    assert template().returncode != 0


def test_defaults_render_workload_and_volume():
    docs = render(*REQUIRED)
    assert set(docs) == {"Deployment", "Service", "PersistentVolumeClaim"}
    dep = docs["Deployment"]
    assert dep["spec"]["strategy"] == {"type": "Recreate"}  # RWO volume: the old pod must release it first
    pod = dep["spec"]["template"]["spec"]
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True, "runAsUser": 65534, "fsGroup": 65534, "seccompProfile": {"type": "RuntimeDefault"},
    }
    c = pod["containers"][0]
    chart = yaml.safe_load(open(f"{CHART}/Chart.yaml"))
    assert chart["version"] == chart["appVersion"]
    assert c["image"] == f"10.34.1.1:30095/daikin-onecta-exporter:{chart['appVersion']}"
    env = {e["name"]: e for e in c["env"]}
    assert set(env) == {"DAIKIN_CLIENT_ID", "DAIKIN_CLIENT_SECRET", "DAIKIN_TOKEN_JSON", "DAIKIN_TOKEN_FILE", "DAIKIN_INTERVAL"}
    for name, key in (("DAIKIN_CLIENT_ID", "client-id"), ("DAIKIN_CLIENT_SECRET", "client-secret"), ("DAIKIN_TOKEN_JSON", "token-json")):
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "daikin-onecta", "key": key}
    assert env["DAIKIN_TOKEN_FILE"]["value"] == "/data/token.json"
    assert env["DAIKIN_INTERVAL"]["value"] == "600"
    assert c["ports"][0]["containerPort"] == 9879
    assert c["securityContext"] == {
        "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
    }
    assert c["readinessProbe"]["tcpSocket"]["port"] == "metrics"
    assert c["livenessProbe"]["httpGet"] == {"path": "/metrics", "port": "metrics"}
    assert c["volumeMounts"] == [{"name": "data", "mountPath": "/data"}]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == docs["PersistentVolumeClaim"]["metadata"]["name"]
    assert docs["Service"]["spec"]["ports"][0]["port"] == 9879
    # the volume holds the live login: helm uninstall must not delete it
    assert docs["PersistentVolumeClaim"]["metadata"]["annotations"] == {"helm.sh/resource-policy": "keep"}
    spec = docs["PersistentVolumeClaim"]["spec"]
    assert spec["accessModes"] == ["ReadWriteOnce"] and spec["resources"]["requests"]["storage"] == "1Gi"
    assert "storageClassName" not in spec


def test_storage_class_and_interval_values():
    docs = render(*REQUIRED, "--set", "persistence.storageClass=ceph-rbd", "--set", "onecta.interval=900")
    assert docs["PersistentVolumeClaim"]["spec"]["storageClassName"] == "ceph-rbd"
    env = {e["name"]: e for e in docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DAIKIN_INTERVAL"]["value"] == "900"


def rules_of(docs):
    return {r["alert"]: r for g in docs["PrometheusRule"]["spec"]["groups"] for r in g["rules"]}


def test_all_toggles():
    all_docs = render_all(*REQUIRED, *TOGGLES)
    docs = {d["kind"]: d for d in all_docs if d["kind"] != "ConfigMap"}
    assert set(docs) == {"Deployment", "Service", "PersistentVolumeClaim", "ServiceMonitor", "PrometheusRule"}
    ep = docs["ServiceMonitor"]["spec"]["endpoints"][0]
    assert (ep["port"], ep["path"], ep["interval"]) == ("metrics", "/metrics", "60s")

    rules = rules_of(docs)
    assert {n: (r["expr"], r["for"]) for n, r in rules.items()} == {
        "DaikinUnreachable": ("daikin_up == 0", "1h"),
        "DaikinLoginFailing": ("daikin_login_ok == 0", "15m"),
        "DaikinLoginExpiring": ("time() - daikin_authorized_timestamp_seconds > 335 * 86400", "1h"),
        "DaikinTokenNotSaved": ("daikin_token_saved == 0", "15m"),
        "DaikinError": ("daikin_error_state == 1", "10m"),
        "DaikinTankCold": ('daikin_temperature_celsius{sensor="tankTemperature"} < 40', "2h"),
    }
    assert all(r["labels"]["severity"] == "warning" for r in rules.values())
    assert "login helper" in rules["DaikinLoginFailing"]["annotations"]["summary"]
    summary = rules["DaikinTokenNotSaved"]["annotations"]["summary"]
    assert "only in memory" in summary and "browser login" in summary and "/data" in summary

    cms = [c for c in all_docs if c["kind"] == "ConfigMap"]
    assert len(cms) == 1 and set(cms[0]["data"]) == {"daikin.json"}
    assert cms[0]["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert cms[0]["metadata"]["annotations"]["grafana_folder"] == "Home"
    assert json.loads(cms[0]["data"]["daikin.json"])["uid"] == "daikin"


def test_tank_min_is_tunable():
    docs = render(*REQUIRED, "--set", "prometheusRule.enabled=true", "--set", "alerts.tankMin=45")
    assert rules_of(docs)["DaikinTankCold"]["expr"].endswith("< 45")


def test_dashboard_with_empty_labels_and_annotations():
    docs = render(*REQUIRED, "--set", "dashboard.enabled=true",
                  "--set-json", "dashboard.labels=null", "--set-json", "dashboard.annotations=null")
    meta = docs["ConfigMap"]["metadata"]
    assert "grafana_dashboard" not in meta["labels"] and "annotations" not in meta


def test_metrics_exist():
    docs = render(*REQUIRED, "--set", "prometheusRule.enabled=true")
    used = set(re.findall(r"daikin_[a-z_]+", json.dumps(docs["PrometheusRule"])))
    assert used and used <= KNOWN, used - KNOWN
    with open(f"{CHART}/dashboards/daikin.json") as f:
        dash = json.load(f)
    assert dash["uid"] == "daikin" and dash["editable"] is False and dash["title"] == "Heat pump"
    panels = [p for p in dash["panels"] if p["type"] != "row"]
    assert len(panels) >= 5
    for p in panels:
        assert p["datasource"] == {"type": "prometheus", "uid": "prometheus"}
        for t in p["targets"]:
            assert t["datasource"]["uid"] == "prometheus"
            used = set(re.findall(r"daikin_[a-z_]+", t["expr"]))
            assert used and used <= KNOWN, used - KNOWN
            # empty legend = exporter-health panel without labels
            legend = t["legendFormat"]
            assert not legend or any(k in legend for k in ("{{management_point}}", "{{sensor}}", "{{mode}}")), legend


def test_dashboard_panel_queries():
    with open(f"{CHART}/dashboards/daikin.json") as f:
        panels = {p["title"]: p for p in json.load(f)["panels"]}
    assert [t["expr"] for t in panels["Mode"]["targets"]] == ["daikin_operation_mode_info == 1"]  # on/off has its own panel
    assert 'setpoint!="leavingWaterOffset"' in panels["Setpoint vs actual"]["targets"][0]["expr"]  # an offset, not degrees
