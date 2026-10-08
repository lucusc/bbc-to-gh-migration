"""Generate a self-contained dashboard from repository migration results."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

DASHBOARD_FILENAME = "migration-dashboard.html"


def _display_name(result: dict, repository_directory: str) -> str:
    source = str(result.get("source", "")).removesuffix("/")
    if source:
        name = source.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    return repository_directory


def collect_latest_results(working_directory: Path) -> list[dict]:
    """Return the newest result for each repository directory."""
    results: list[dict] = []
    if not working_directory.exists():
        return results

    for repository_directory in sorted(path for path in working_directory.iterdir() if path.is_dir()):
        result_paths = sorted(repository_directory.glob("*/result.json"), reverse=True)
        if not result_paths:
            continue

        result_path = result_paths[0]
        run_directory = result_path.parent
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("result.json must contain a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result = {
                "runId": run_directory.name,
                "status": "Unreadable",
                "error": f"Could not read result.json: {exc}",
            }

        if result.get("status") == "SignedOff" and not result.get("verifiedRefs"):
            for prior_path in result_paths[1:]:
                try:
                    prior = json.loads(prior_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if prior.get("verifiedRefs"):
                    for key in (
                        "verifiedRefs",
                        "sourceRefCount",
                        "destinationRefCount",
                        "sourceLfsObjectCount",
                        "destinationLfsObjectCount",
                        "usesLfs",
                    ):
                        if key in prior:
                            result[key] = prior[key]
                    result["verificationRunId"] = prior.get("runId")
                    break

        result["repository"] = _display_name(result, repository_directory.name)
        result["repositoryDirectory"] = repository_directory.name
        relative_run = f"{quote(repository_directory.name)}/{quote(run_directory.name)}"
        result["resultHref"] = f"{relative_run}/result.json"
        log_path = run_directory / "migration.log"
        result["logHref"] = f"{relative_run}/migration.log" if log_path.exists() else None
        results.append(result)

    return sorted(
        results,
        key=lambda result: (
            str(result.get("completedAt") or result.get("failedAt") or ""),
            str(result.get("runId") or ""),
        ),
        reverse=True,
    )


def generate_dashboard(working_directory: Path | str) -> Path:
    """Write a standalone HTML dashboard containing the latest repository results."""
    output_directory = Path(working_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / DASHBOARD_FILENAME
    payload = json.dumps(collect_latest_results(output_directory), ensure_ascii=True).replace(
        "<", "\\u003c"
    )
    output_path.write_text(_dashboard_html(payload), encoding="utf-8")
    return output_path


def _dashboard_html(payload: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Repository migration status</title>
  <script>
    (() => {{
      const param = new URLSearchParams(window.location.search).get("scoutTheme");
      const theme =
        param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.setAttribute("data-theme", theme);
    }})();
  </script>
  <style>
    :root {{
      color-scheme: light;
      --cp-bg: #f7f4ef;
      --cp-bg-elevated: #fcfbf8;
      --cp-surface: #ffffff;
      --cp-surface-soft: #f5f5f5;
      --cp-border: #dedede;
      --cp-border-strong: #919191;
      --cp-text: #242424;
      --cp-text-muted: #5c5c5c;
      --cp-text-soft: #6f6f6f;
      --cp-accent: #b11f4b;
      --cp-accent-hover: #9a1a41;
      --cp-accent-soft: rgba(177, 31, 75, 0.08);
      --cp-accent-fg: #ffffff;
      --cp-success: #16a34a;
      --cp-danger: #dc2626;
      --cp-warning: #f59e0b;
      --cp-link: #0078d4;
      --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
      --cp-overlay: rgba(255, 255, 255, 0.8);
      --cp-panel: rgba(255, 255, 255, 0.86);
      --cp-panel-strong: rgba(255, 255, 255, 0.96);
      --cp-sheen: rgba(255, 255, 255, 0.55);
      --cp-highlight: rgba(177, 31, 75, 0.12);
    }}
    html[data-theme="dark"] {{
      color-scheme: dark;
      --cp-bg: #3d3b3a;
      --cp-bg-elevated: #343231;
      --cp-surface: #292929;
      --cp-surface-soft: #2e2e2e;
      --cp-border: #474747;
      --cp-border-strong: #5f5f5f;
      --cp-text: #dedede;
      --cp-text-muted: #919191;
      --cp-text-soft: #b0b0b0;
      --cp-accent: #fd8ea1;
      --cp-accent-hover: #fb7b91;
      --cp-accent-soft: rgba(253, 142, 161, 0.14);
      --cp-accent-fg: #1a1a1a;
      --cp-success: #4ade80;
      --cp-danger: #f87171;
      --cp-warning: #fbbf24;
      --cp-link: #4da6ff;
      --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
      --cp-overlay: rgba(41, 41, 41, 0.88);
      --cp-panel: rgba(41, 41, 41, 0.72);
      --cp-panel-strong: rgba(41, 41, 41, 0.96);
      --cp-sheen: rgba(255, 255, 255, 0.04);
      --cp-highlight: rgba(253, 142, 161, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--cp-bg);
      color: var(--cp-text);
      font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    a {{ color: var(--cp-link); }}
    button, input, select {{ font: inherit; }}
    .shell {{ width: min(1200px, calc(100% - 32px)); margin: 0 auto; padding: 40px 0 56px; }}
    header {{ display: flex; gap: 24px; justify-content: space-between; align-items: end; margin-bottom: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: clamp(1.75rem, 4vw, 2.5rem); letter-spacing: -0.04em; }}
    header p, .muted {{ color: var(--cp-text-muted); }}
    header p {{ margin: 0; }}
    .timestamp {{ text-align: right; white-space: nowrap; font-size: 0.875rem; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }}
    .metric {{ background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px; padding: 20px; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 1.75rem; }}
    .controls {{ display: grid; grid-template-columns: minmax(220px, 1fr) 220px; gap: 12px; margin-bottom: 16px; }}
    .control {{
      width: 100%;
      background: var(--cp-surface);
      color: var(--cp-text);
      border: 1px solid var(--cp-border-strong);
      border-radius: 0.625rem;
      padding: 10px 12px;
    }}
    .list {{ display: grid; gap: 12px; }}
    .repo-card {{ background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px; overflow: hidden; }}
    .repo-main {{ display: grid; grid-template-columns: minmax(180px, 1.25fr) minmax(180px, 1fr) minmax(160px, 0.8fr) auto; gap: 20px; align-items: center; padding: 20px; }}
    .repo-name {{ margin: 0 0 6px; font-size: 1.125rem; overflow-wrap: anywhere; }}
    .label {{ display: block; margin-bottom: 5px; color: var(--cp-text-muted); font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }}
    .value {{ overflow-wrap: anywhere; }}
    .status {{ display: inline-flex; align-items: center; gap: 8px; font-size: 0.875rem; font-weight: 700; }}
    .status::before {{ content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--cp-text-muted); }}
    .status.success::before {{ background: var(--cp-success); }}
    .status.warning::before {{ background: var(--cp-warning); }}
    .status.danger::before {{ background: var(--cp-danger); }}
    .details-button {{
      border: 1px solid var(--cp-border-strong);
      border-radius: 0.625rem;
      padding: 8px 12px;
      background: var(--cp-surface-soft);
      color: var(--cp-text);
      cursor: pointer;
    }}
    .details-button:hover {{ border-color: var(--cp-accent); }}
    .repo-details {{ border-top: 1px solid var(--cp-border); padding: 20px; background: var(--cp-bg-elevated); }}
    .detail-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin-bottom: 16px; }}
    .message {{ margin: 0 0 16px; padding: 12px; border-left: 3px solid var(--cp-accent); background: var(--cp-accent-soft); white-space: pre-wrap; overflow-wrap: anywhere; }}
    .ref-sections {{ display: grid; gap: 16px; margin: 20px 0; }}
    .ref-section {{ border: 1px solid var(--cp-border); border-radius: 0.625rem; overflow: hidden; }}
    .ref-heading {{ display: flex; justify-content: space-between; gap: 16px; margin: 0; padding: 10px 12px; background: var(--cp-surface-soft); font-size: 0.875rem; }}
    .ref-list {{ margin: 0; padding: 0; list-style: none; }}
    .ref-row {{ display: grid; grid-template-columns: minmax(140px, 1fr) minmax(220px, 1.5fr); gap: 16px; padding: 9px 12px; border-top: 1px solid var(--cp-border); }}
    .ref-hash {{ color: var(--cp-text-muted); overflow-wrap: anywhere; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 16px; }}
    .empty {{ padding: 48px 24px; text-align: center; background: var(--cp-surface); border: 1px dashed var(--cp-border-strong); border-radius: 16px; }}
    code {{ font-family: Consolas, "Courier New", Courier, monospace; font-size: 0.875em; }}
    @media (max-width: 820px) {{
      header {{ display: block; }}
      .timestamp {{ margin-top: 12px; text-align: left; }}
      .summary {{ grid-template-columns: repeat(2, 1fr); }}
      .repo-main {{ grid-template-columns: 1fr 1fr; }}
      .detail-grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    @media (max-width: 520px) {{
      .shell {{ width: min(100% - 20px, 1200px); padding-top: 24px; }}
      .controls, .repo-main, .detail-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>Repository migration status</h1>
        <p>Latest recorded outcome for each repository.</p>
      </div>
      <div class="timestamp muted" id="generated-at"></div>
    </header>
    <section class="summary" aria-label="Migration summary">
      <div class="metric"><span class="muted">Repositories</span><strong id="total-count">0</strong></div>
      <div class="metric"><span class="muted">Verified</span><strong id="verified-count">0</strong></div>
      <div class="metric"><span class="muted">Signed off</span><strong id="signed-count">0</strong></div>
      <div class="metric"><span class="muted">Needs attention</span><strong id="attention-count">0</strong></div>
    </section>
    <section class="controls" aria-label="Dashboard filters">
      <input class="control" id="search" type="search" placeholder="Search repositories, owners, or destinations" aria-label="Search migration results">
      <select class="control" id="status-filter" aria-label="Filter by status">
        <option value="">All statuses</option>
      </select>
    </section>
    <section class="list" id="repo-list" aria-live="polite"></section>
  </main>
  <script>
    const RUNS = {payload};
    const generatedAt = new Date();
    const list = document.getElementById("repo-list");
    const search = document.getElementById("search");
    const statusFilter = document.getElementById("status-filter");

    function text(value, fallback = "\u2014") {{
      return value === null || value === undefined || value === "" ? fallback : String(value);
    }}

    function statusClass(run) {{
      if (run.status === "SignedOff" || run.status === "VerifiedPendingOwnerSignOff") return "success";
      if (run.status === "Reconciled") return run.exactMatch ? "success" : "warning";
      if (run.status === "Failed" || run.status === "Unreadable") return "danger";
      return "warning";
    }}

    function statusLabel(status) {{
      return status === "NotApplicable" ? "Not applicable" : text(status);
    }}

    function timeFor(run) {{
      const value = run.completedAt || run.failedAt;
      if (!value) return "\u2014";
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
    }}

    function add(parent, tag, className, value) {{
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (value !== undefined) element.textContent = value;
      parent.appendChild(element);
      return element;
    }}

    function field(parent, label, value) {{
      const wrapper = add(parent, "div");
      add(wrapper, "span", "label", label);
      add(wrapper, "div", "value", text(value));
      return wrapper;
    }}

    function renderRefs(parent, run) {{
      const inventory = run.verifiedRefs;
      if (!inventory) return;
      const sections = add(parent, "div", "ref-sections");
      const groups = [
        ["Verified branches", inventory.branches],
        ["Verified tags", inventory.tags],
        ["Verified other refs", inventory.other]
      ];
      groups.forEach(([title, refs]) => {{
        if (!Array.isArray(refs) || !refs.length) return;
        const section = add(sections, "section", "ref-section");
        const heading = add(section, "h3", "ref-heading");
        add(heading, "span", "", title);
        add(heading, "span", "muted", String(refs.length));
        const rows = add(section, "ul", "ref-list");
        refs.forEach((ref) => {{
          const row = add(rows, "li", "ref-row");
          add(row, "code", "", text(ref.name));
          const hashes = ref.peeledHash
            ? `${{text(ref.hash, "tag object unavailable")}} \u2192 ${{ref.peeledHash}}`
            : text(ref.hash);
          add(row, "code", "ref-hash", hashes);
        }});
      }});
    }}

    function renderCard(run) {{
      const card = add(list, "article", "repo-card");
      const main = add(card, "div", "repo-main");
      const identity = add(main, "div");
      add(identity, "h2", "repo-name", text(run.repository));
      add(identity, "div", "muted", text(run.owner, "Owner not recorded"));

      const outcome = add(main, "div");
      add(outcome, "span", "label", "Outcome");
      add(outcome, "div", `status ${{statusClass(run)}}`, statusLabel(run.status));
      field(main, "Last run", `${{timeFor(run)}} \u00b7 ${{text(run.operation, "operation not recorded")}}`);

      const button = add(main, "button", "details-button", "Details");
      button.type = "button";
      button.setAttribute("aria-expanded", "false");

      const details = add(card, "div", "repo-details");
      details.hidden = true;
      const grid = add(details, "div", "detail-grid");
      field(grid, "Source", run.source);
      field(grid, "Destination", run.destination);
      field(grid, "Source refs", run.sourceRefCount);
      field(grid, "Destination refs", run.destinationRefCount);
      field(grid, "LFS", run.usesLfs ? "Enabled" : "Not enabled");
      field(grid, "Verified LFS objects", run.destinationLfsObjectCount);
      field(grid, "Run ID", run.runId);
      field(grid, "Approval reference", run.approvalReference);

      const messages = [];
      if (run.error) messages.push(run.error);
      if (run.reason) messages.push(run.reason);
      if (Array.isArray(run.differences) && run.differences.length) messages.push(run.differences.join("\\n"));
      if (messages.length) add(details, "p", "message", messages.join("\\n\\n"));
      renderRefs(details, run);

      const links = add(details, "div", "links");
      const resultLink = add(links, "a", "", "View result JSON");
      resultLink.href = run.resultHref;
      if (run.logHref) {{
        const logLink = add(links, "a", "", "View migration log");
        logLink.href = run.logHref;
      }}

      button.addEventListener("click", () => {{
        details.hidden = !details.hidden;
        button.setAttribute("aria-expanded", String(!details.hidden));
        button.textContent = details.hidden ? "Details" : "Hide details";
      }});
    }}

    function render() {{
      const query = search.value.trim().toLowerCase();
      const status = statusFilter.value;
      const visible = RUNS.filter((run) => {{
        const refNames = Object.values(run.verifiedRefs || {{}})
          .flat().map((ref) => ref.name);
        const searchable = [run.repository, run.owner, run.source, run.destination, run.status, ...refNames]
          .filter(Boolean).join(" ").toLowerCase();
        return (!query || searchable.includes(query)) && (!status || run.status === status);
      }});
      list.replaceChildren();
      if (!visible.length) {{
        add(list, "div", "empty", RUNS.length ? "No migration results match these filters." : "No migration results have been recorded yet.");
        return;
      }}
      visible.forEach(renderCard);
    }}

    const statuses = [...new Set(RUNS.map((run) => run.status).filter(Boolean))].sort();
    statuses.forEach((status) => {{
      const option = document.createElement("option");
      option.value = status;
      option.textContent = status;
      statusFilter.appendChild(option);
    }});
    document.getElementById("generated-at").textContent = `Dashboard generated ${{generatedAt.toLocaleString()}}`;
    document.getElementById("total-count").textContent = RUNS.length;
    document.getElementById("verified-count").textContent = RUNS.filter((run) => run.status === "VerifiedPendingOwnerSignOff").length;
    document.getElementById("signed-count").textContent = RUNS.filter((run) => run.status === "SignedOff").length;
    document.getElementById("attention-count").textContent = RUNS.filter((run) =>
      run.status === "Failed" || run.status === "Unreadable" || (run.status === "Reconciled" && !run.exactMatch)
      || run.status === "NotApplicable"
    ).length;
    search.addEventListener("input", render);
    statusFilter.addEventListener("change", render);
    render();
  </script>
</body>
</html>
"""
