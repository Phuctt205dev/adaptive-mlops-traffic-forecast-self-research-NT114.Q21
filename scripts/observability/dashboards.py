"""Minimal admin dashboards. Build JSON only; never call Grafana or Kubernetes."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "deployment/observability/dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}
NODES = ("mlops-node-01", "mlops-node-02", "mlops-node-03")
SERVICES = (
    ("Prediction API", "api|api-next|traffic-api", 2),
    ("Tree training", "train-tree|tree-next|traffic-training-tree", 1),
    ("Deep training", "train-neural|neural-next|traffic-training-worker", 1),
    ("Database", "postgres", 1), ("Dataset storage", "minio", 1),
    ("Model registry", "mlflow", 1), ("Airflow UI", "airflow-webserver", 1),
    ("Airflow scheduler", "airflow-scheduler", 1),
)


def fresh(selector):
    return f"({selector} and (time() - timestamp({selector}) <= 180))"


def live(selector, job):
    scraper = fresh('up{job="' + job + '"}')
    return f"({fresh(selector)} and on (instance, job) ({scraper} == 1))"


def named(expression, node=None):
    selector = 'node_uname_info{job="node-exporter"' + (f',nodename="{node}"' if node else "") + "}"
    identity = "max by (instance, job, nodename) (" + live(selector, "node-exporter") + ")"
    return "max by (nodename) ((" + expression + ") * on (instance, job) group_left(nodename) (" + identity + "))"


def resources(node=None):
    cpu = 'node_cpu_seconds_total{job="node-exporter",mode="idle"}'
    memory = 'node_memory_MemAvailable_bytes{job="node-exporter"}'
    total = 'node_memory_MemTotal_bytes{job="node-exporter"}'
    disk = 'job="node-exporter",mountpoint="/",fstype!~"tmpfs|overlay|squashfs"'
    available = "node_filesystem_avail_bytes{" + disk + "}"
    size = "node_filesystem_size_bytes{" + disk + "}"
    return {
        "Processing": named(f"100 * (1 - avg by(instance, job)(rate({cpu}[5m]) and {fresh(cpu)}))", node),
        "Memory": named("100 * (1 - " + live(memory, "node-exporter") + " / " + live(total, "node-exporter") + ")", node),
        "Storage": named("100 * (1 - " + live(available, "node-exporter") + " / " + live(size, "node-exporter") + ")", node),
        "Memory free": named(live(memory, "node-exporter"), node),
        "Storage free": named(live(available, "node-exporter"), node),
    }


def node_ready(node=None):
    selector = 'kube_node_status_condition{job="kube-state-metrics",condition="Ready",status="true"'
    selector += (f',node="{node}"' if node else "") + "}"
    return 'max by(nodename)(label_replace(max by(node)(' + live(selector, "kube-state-metrics") + '),"nodename","$1","node","(.*)"))'


def service_state(pattern, expected):
    labels = f'job="kube-state-metrics",namespace="traffic-mlops",deployment=~"{pattern}"'
    desired = "max by(deployment)(" + live("kube_deployment_spec_replicas{" + labels + "}", "kube-state-metrics") + ")"
    ready = "max by(deployment)(" + live("kube_deployment_status_replicas_ready{" + labels + "}", "kube-state-metrics") + ")"
    # Any fully ready active slot can serve this component during blue/green.
    # Missing samples remain Unknown; an intentionally stopped slot is not Ready.
    return f"((2 - max(({ready} >= bool {desired}) * ({desired} >= bool {expected}))) or vector(0))"


def inventory_status(expressions, expected):
    ready = " + ".join(f"(sum(({expression}) == bool 1))" for expression in expressions)
    present = " + ".join(f"(sum(({expression}) > bool 0))" for expression in expressions)
    return f"((2 - (({ready}) == bool {expected})) and on() (({present}) == {expected})) or vector(0)"


def resource_status(node=None):
    metrics = resources(node)
    names = ("Processing", "Memory", "Storage")
    expected = 1 if node else len(NODES)
    known = " and on() ".join(f"(count({metrics[name]}) == {expected})" for name in names)
    high = " + ".join(f"(max({metrics[name]}) >= bool 80)" for name in names)
    return f"((1 + (({high}) > bool 0)) and on() {known}) or vector(0)"


def status_mappings(attention="Needs attention"):
    return [{"type": "value", "options": {
        "0": {"text": "Unknown", "color": "gray"}, "1": {"text": "Ready", "color": "green"},
        "2": {"text": attention, "color": "orange"}, "3": {"text": "Not ready", "color": "red"}}},
        {"type": "special", "options": {"match": "null", "result": {"text": "Unknown", "color": "gray"}}}]


def thresholds(red=90):
    return {"mode": "absolute", "steps": [{"color": "green", "value": None},
        {"color": "orange", "value": 80}, {"color": "red", "value": red}]}


def target(expression, ref="A", *, legend="", trend=False, table=False):
    return {"refId": ref, "datasource": copy.deepcopy(DS), "expr": expression,
        "format": "table" if table else "time_series", "instant": not trend, "range": trend,
        "editorMode": "code", "legendFormat": legend, "exemplar": False}


def panel(identifier, title, kind, x, y, width, height, description=""):
    return {"id": identifier, "title": title, "type": kind, "description": description,
        "datasource": copy.deepcopy(DS), "gridPos": {"x": x, "y": y, "w": width, "h": height},
        "fieldConfig": {"defaults": {"noValue": "Unknown", "decimals": 0, "mappings": [
            {"type": "special", "options": {"match": "null", "result": {"text": "Unknown", "color": "gray"}}}]}, "overrides": []}}


def stat(identifier, title, expression, x, y, width, height=4, *, status=False, description=""):
    result = panel(identifier, title, "stat", x, y, width, height, description)
    result["targets"] = [target(expression)]
    result["fieldConfig"]["defaults"].update({"color": {"mode": "thresholds"}, "thresholds": thresholds()})
    if status:
        result["fieldConfig"]["defaults"]["mappings"] = status_mappings()
    result["options"] = {"colorMode": "value", "graphMode": "none", "justifyMode": "center",
        "textMode": "value", "orientation": "auto", "wideLayout": True,
        "reduceOptions": {"values": False, "calcs": ["last"], "fields": ""}, "text": {"valueSize": 28}}
    return result


def document(uid, title, panels, peer_uid, peer_title):
    return {"id": None, "uid": uid, "title": title, "schemaVersion": 42, "version": 0,
        "description": "Kubernetes readiness and resource usage. Unknown means missing or stale metrics. Open panel info for definitions.",
        "editable": True, "tags": ["traffic-mlops", "rke2", "minimal"], "timezone": "browser",
        "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m"]},
        "annotations": {"list": []}, "templating": {"list": []},
        "links": [{"title": peer_title, "type": "link", "url": "/d/" + peer_uid,
                   "targetBlank": False, "keepTime": True}], "panels": panels}


def build_system():
    states = [service_state(pattern, expected) for _, pattern, expected in SERVICES]
    services = inventory_status(states, len(SERVICES))
    ready_nodes = [f"((2 - max({node_ready(node)})) or vector(0))" for node in NODES]
    nodes = inventory_status(ready_nodes, len(NODES))
    infrastructure = inventory_status([nodes, services], 2)
    ready_service_count = " + ".join(f"(({s}) == bool 1)" for s in states)
    panels = [stat(1, "Platform readiness", infrastructure, 0, 0, 8, 5, status=True,
        description="All three nodes and eight application components meet Kubernetes readiness. This does not test HTTP requests, forecast accuracy, or training success."),
        stat(2, "Nodes", f"sum({node_ready()})", 8, 0, 4, 5,
             description="Ready nodes out of three. Unknown means missing collection data."),
        stat(3, "Services", f"(({ready_service_count}) and on() (({services}) > 0))", 12, 0, 4, 5,
             description="Ready application components out of eight; active and next slots count as the same service."),
        stat(4, "Resources", resource_status(), 16, 0, 4, 5, status=True,
             description="Review a node when processing, memory or root storage usage reaches 80%. Missing data on any expected node is Unknown.")]
    panels[3]["fieldConfig"]["defaults"]["mappings"] = status_mappings("High use")
    panels[3]["fieldConfig"]["defaults"]["mappings"][0]["options"]["1"]["text"] = "Within range"
    panels[3]["options"]["text"]["valueSize"] = 22
    for item, maximum in ((panels[1], len(NODES)), (panels[2], len(SERVICES))):
        item["fieldConfig"]["defaults"].update({"unit": "none", "thresholds": {
            "mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": maximum}]}})
        item["fieldConfig"]["defaults"]["unit"] = "suffix: / " + str(maximum)
    monitor = fresh('up{job="traffic-monitoring-prometheus",container="prometheus"}')
    warnings = '(count(ALERTS{alertstate="firing",severity=~"warning|critical"}) or vector(0))'
    critical = '(count(ALERTS{alertstate="firing",severity="critical"}) or vector(0))'
    alert_state = f"((1 + ({warnings} > bool 0) + ({critical} > bool 0)) and on() (max({monitor}) == 1)) or vector(0)"
    alert = stat(5, "Monitoring", alert_state, 20, 0, 4, 5, status=True,
        description="Prometheus warning/critical alerts. Informational and heartbeat alerts are excluded. This is monitoring state, separate from application readiness.")
    alert["fieldConfig"]["defaults"]["mappings"] = status_mappings("Review alerts")
    alert["fieldConfig"]["defaults"]["mappings"][0]["options"]["1"]["text"] = "No warnings"
    alert["fieldConfig"]["defaults"]["mappings"][0]["options"]["3"]["text"] = "Critical alert"
    alert["options"]["text"]["valueSize"] = 22
    panels.append(alert)
    for index, (name, _, _) in enumerate(SERVICES):
        service = stat(10 + index, name, states[index], (index % 4) * 6, 5 + (index // 4) * 3, 6, 3, status=True,
            description="Kubernetes replicas are ready for this component. This is not a training result or business health check.")
        service["options"]["text"]["valueSize"] = 22
        service["fieldConfig"]["defaults"]["mappings"] = status_mappings("Not ready")
        service["fieldConfig"]["defaults"]["mappings"][0]["options"]["2"]["color"] = "red"
        panels.append(service)
    metrics = resources()
    for index, name in enumerate(("Processing", "Memory", "Storage")):
        result = stat(20 + index, name + " • busiest node", "topk(1," + metrics[name] + ")", index * 8, 11, 8, 5,
            description="Highest measured node usage; the node name is shown. Missing nodes are excluded here; Resources above requires all nodes. CPU is a five-minute mean; storage is the root filesystem.")
        result["fieldConfig"]["defaults"].update({"unit": "percent", "min": 0, "max": 100, "thresholds": thresholds(95 if index == 0 else 90)})
        result["targets"][0]["legendFormat"] = "{{nodename}}"
        result["options"].update(textMode="value_and_name", text={"valueSize": 36, "titleSize": 16})
        result["fieldConfig"]["defaults"]["links"] = [{"title": "View nodes", "url": "/d/traffic-rke2-nodes"}]
        panels.append(result)
    details = panel(31, "Alerts to review", "table", 0, 17, 24, 7,
        "Warning/critical alerts reported by Prometheus. No rows may mean no matching alerts; check Monitoring above for collection state.")
    details["targets"] = [target('ALERTS{alertstate="firing",severity=~"warning|critical"}', table=True)]
    details["options"] = {"showHeader": True, "cellHeight": "lg", "footer": {"show": False}}
    details["transformations"] = [{"id": "filterFieldsByName", "options": {"include": {"names": ["alertname", "severity", "namespace", "pod"]}}},
        {"id": "organize", "options": {"renameByName": {"alertname": "What to review", "severity": "Priority", "namespace": "Area", "pod": "Component"}}}]
    details["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "What to review"}, "properties": [{"id": "mappings", "value": [
        {"type": "value", "options": {"PrometheusNotConnectedToAlertmanagers": {"text": "Alert delivery is not connected"}}}]}]}]
    panels.append({"id": 30, "title": "Alert details", "type": "row", "collapsed": True,
                   "gridPos": {"x": 0, "y": 16, "w": 24, "h": 1}, "panels": [details]})
    return document("traffic-system-overview", "System Overview", panels, "traffic-rke2-nodes", "Nodes")


def build_nodes():
    panels = []
    for index, node in enumerate(NODES):
        metrics = resources(node)
        ready = f"max({node_ready(node)})"
        resource = resource_status(node)
        state = f"(((3 - 2 * ({ready}) + (({resource}) - 1) * ({ready})) and on() (({resource}) > 0)) or (3 + 0 * (({ready}) == 0)) or vector(0))"
        role = "System" if index == 0 else "Applications"
        status = stat(1 + index * 3, f"Node {index + 1:02d} · {role}", state, index * 8, 0, 8, 4, status=True,
            description=node + ". " + ("Control plane and monitoring." if index == 0 else "Application services and training controller.") + " High use is 80% or more; it does not by itself prove an outage.")
        status["fieldConfig"]["defaults"]["mappings"] = status_mappings("High use")
        panels.append(status)
        bars = panel(2 + index * 3, f"Node {index + 1:02d} · Resources", "bargauge", index * 8, 4, 8, 7,
            "Processing: non-idle CPU, five-minute average. Memory: 1-MemAvailable/MemTotal. Storage: 1-avail/size for /. Review at 80%; high at 95% CPU or 90% memory/storage.")
        bars["targets"] = [target(metrics[name], chr(65 + i), legend=name) for i, name in enumerate(("Processing", "Memory", "Storage"))]
        bars["fieldConfig"]["defaults"].update({"unit": "percent", "min": 0, "max": 100, "color": {"mode": "thresholds"}, "thresholds": thresholds()})
        bars["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "Processing"}, "properties": [{"id": "thresholds", "value": thresholds(95)}]}]
        bars["options"] = {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
            "valueMode": "color", "namePlacement": "top", "sizing": "auto", "minVizHeight": 24,
            "reduceOptions": {"values": False, "calcs": ["last"], "fields": ""}}
        panels.append(bars)
        free = panel(3 + index * 3, f"Node {index + 1:02d} · Available", "stat", index * 8, 11, 8, 3,
            "Reclaimable memory available to Linux, and root filesystem space available to normal users. These are not Kubernetes scheduling reservations.")
        free["targets"] = [target(metrics[name], chr(65 + i), legend=name) for i, name in enumerate(("Memory free", "Storage free"))]
        free["fieldConfig"]["defaults"].update({"unit": "bytes", "decimals": 1, "color": {"mode": "fixed", "fixedColor": "text"}})
        free["options"] = {"colorMode": "none", "graphMode": "none", "textMode": "value_and_name", "orientation": "horizontal", "justifyMode": "center",
            "reduceOptions": {"values": False, "calcs": ["last"], "fields": ""}, "text": {"valueSize": 23, "titleSize": 13}}
        panels.append(free)
    graphs = []
    for index, name in enumerate(("Processing", "Memory", "Storage")):
        graph = panel(41 + index, name + " history", "timeseries", index * 8, 15, 8, 7,
            "One line per node. Missing samples remain gaps; the lines are not stacked.")
        graph["targets"] = [target(resources()[name], trend=True, legend="{{nodename}}")]
        graph["fieldConfig"]["defaults"].update({"unit": "percent", "min": 0, "max": 100,
            "color": {"mode": "palette-classic"}, "custom": {"lineWidth": 2, "fillOpacity": 0,
                "showPoints": "never", "spanNulls": False, "axisGridShow": False, "stacking": {"mode": "none", "group": "A"}}})
        graph["options"] = {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
        graph["maxDataPoints"] = 180
        graphs.append(graph)
    panels.append({"id": 40, "title": "Usage history", "type": "row", "collapsed": True,
                   "gridPos": {"x": 0, "y": 14, "w": 24, "h": 1}, "panels": graphs})
    return document("traffic-rke2-nodes", "Nodes", panels, "traffic-system-overview", "System Overview")


def build():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, function in (("system-overview", build_system), ("nodes", build_nodes)):
        path = OUTPUT / (name + ".json")
        path.write_text(json.dumps(function(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    build()
