import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { Link, useParams } from "react-router-dom";

import { fetchProject } from "../api/client";
import type { Denial, ProjectDetail } from "../api/types";
import {
  EnabledBadge,
  MetaRow,
  OptionalText,
  TierBadge,
} from "../components/display";
import ErrorState from "../components/ErrorState";
import LoadingState from "../components/LoadingState";
import MarkdownPanel from "../components/MarkdownPanel";
import { RunHistory, RunRecordDetail } from "../components/RunRecord";

/**
 * The candidate/exempt/blocking-lines/clean_for detail of the Target tree
 * panel's dirty branch — the "which dirt, why" facts. Extracted at CBN-164 to
 * share with the `ignored` branch CBN-188.3 retired; kept as the dirty
 * branch's one consumer rather than re-inlined.
 */
function TreeBlockingDetail({
  tree,
}: {
  tree: NonNullable<ProjectDetail["tree"]>;
}) {
  return (
    <>
      <dl>
        <MetaRow label="Candidate">
          <span className="font-mono text-xs">
            {tree.candidate ?? "(none eligible)"}
          </span>
        </MetaRow>
        <MetaRow label="Exempt">
          <span className="font-mono text-xs">
            {tree.exempt_task ?? "— (candidate has no open tasknote)"}
          </span>
        </MetaRow>
      </dl>
      {tree.blocking_lines.length > 0 ? (
        <ul className="space-y-2 py-2">
          {tree.blocking_lines.map((line) => (
            <li key={line.text} className="text-sm">
              <pre className="overflow-x-auto rounded bg-slate-50 px-2 py-1 font-mono text-xs text-slate-500">
                {line.text.trim()}
              </pre>
              {line.task_id ? (
                <div className="mt-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">
                  Open tasknote for{" "}
                  <span className="font-mono">{line.task_id}</span> — dirt
                  only because the candidate is{" "}
                  <span className="font-mono">
                    {tree.candidate ?? "(none eligible)"}
                  </span>
                  .
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {tree.clean_for ? (
        <p className="pb-2 text-xs text-slate-500">
          The gate reads clean only for a dispatch of{" "}
          <span className="font-mono text-slate-800">{tree.clean_for}</span>.
        </p>
      ) : null}
    </>
  );
}

/**
 * One coverage denial list — reason, the row as it reads, the unparsed-blocker
 * warning, and the seeded row (CBN-119.3, CBN-128). Extracted at CBN-440 so
 * the `obvious` / `needs judgment` groups render the same rows; a
 * `needs-judgment` row adds its `triage:` reason after the row text, an
 * `obvious` one adds none — `_denial_lines`'s rule in `status_render.py`.
 */
function DenialList({
  denials,
  labelledBy,
}: {
  denials: Denial[];
  labelledBy?: string;
}) {
  return (
    <ul className="space-y-2 py-2" aria-labelledby={labelledBy}>
      {denials.map((denial) => (
        <li key={denial.task_id} className="text-sm">
          <div>
            {denial.line > 0 ? (
              <span className="font-mono text-xs text-slate-500">
                line {denial.line}{" "}
              </span>
            ) : null}
            <span className="font-mono text-xs text-slate-800">
              {denial.task_id}
            </span>{" "}
            <span className="text-slate-500">{denial.reason}</span>
          </div>
          {denial.text ? (
            <pre className="mt-1 overflow-x-auto rounded bg-slate-50 px-2 py-1 font-mono text-xs text-slate-500">
              {denial.text.trim()}
            </pre>
          ) : null}
          {denial.triage === "needs-judgment" && denial.triage_reason ? (
            <div className="mt-1 text-xs text-slate-500">
              <span className="font-medium">triage:</span>{" "}
              {denial.triage_reason}
            </div>
          ) : null}
          {denial.unparsed_blocker ? (
            <div className="mt-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">
              <span className="font-medium">
                Says &ldquo;blocked&rdquo;, but no{" "}
                <code className="font-mono">Blocked by [[ID]]</code> clause
                parsed — check before seeding:
              </span>{" "}
              <span className="font-mono">{denial.unparsed_blocker}</span>
            </div>
          ) : null}
          {denial.seeded_line ? (
            <pre className="mt-1 overflow-x-auto rounded bg-emerald-50 px-2 py-1 font-mono text-xs text-emerald-800">
              {denial.seeded_line.trim()}
            </pre>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export default function ProjectDetailPage() {
  const { id = "" } = useParams();
  const { data, error, isLoading } = useQuery({
    queryKey: ["project", id],
    queryFn: () => fetchProject(id),
    enabled: Boolean(id),
  });

  if (isLoading) return <LoadingState label="Loading project…" />;
  if (error) {
    if (isAxiosError(error) && error.response?.status === 404) {
      return (
        <div className="space-y-4">
          <ErrorState message={`Unknown project: ${id}`} />
          <Link to="/" className="text-sm text-sky-700 hover:underline">
            ← Back to fleet
          </Link>
        </div>
      );
    }
    const message =
      error instanceof Error ? error.message : "Failed to load project.";
    return <ErrorState message={message} />;
  }
  if (!data) return null;

  const current = data.current;
  const coverage = data.coverage;
  const tree = data.tree;
  // `format_coverage`'s grouping (CBN-157): obvious rows first, then every
  // other denial, whatever its code, under needs judgment.
  const obvious = (coverage?.denials ?? []).filter(
    (d) => d.triage === "obvious",
  );
  const needsJudgment = (coverage?.denials ?? []).filter(
    (d) => d.triage !== "obvious",
  );

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="text-sm text-sky-700 hover:underline">
          ← Fleet
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">
          {data.id}
        </h1>
        <p className="mt-1 font-mono text-xs text-slate-500">{data.path}</p>
      </div>

      <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <h2 className="py-2 text-sm font-semibold text-slate-800">Registry</h2>
        <dl>
          <MetaRow label="Tier">
            <TierBadge tier={data.tier} />
          </MetaRow>
          <MetaRow label="Enabled">
            <EnabledBadge enabled={data.enabled} />
          </MetaRow>
          <MetaRow label="Provider">
            <OptionalText value={data.provider ?? null} />
          </MetaRow>
          <MetaRow label="Providers">
            <OptionalText value={data.providers?.join(", ") ?? null} />
          </MetaRow>
          <MetaRow label="Model">
            <OptionalText value={data.model ?? null} />
          </MetaRow>
          <MetaRow label="Registered">
            <OptionalText value={data.registered_at ?? null} />
          </MetaRow>
        </dl>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <h2 className="py-2 text-sm font-semibold text-slate-800">
          Current task
        </h2>
        {!current ? (
          <p className="pb-2 text-sm text-slate-500">
            No in-progress Flaitron task.
          </p>
        ) : (
          <dl>
            <MetaRow label="Task">
              <span className="font-mono text-xs">{current.task_id}</span>
            </MetaRow>
            <MetaRow label="Status">
              <OptionalText value={current.status} />
            </MetaRow>
            <MetaRow label="Reason">
              <OptionalText value={current.reason} />
            </MetaRow>
            <MetaRow label="Shortname">
              <OptionalText value={current.shortname ?? null} />
            </MetaRow>
            <MetaRow label="Plan model">
              <OptionalText value={current.plan_model ?? null} />
            </MetaRow>
          </dl>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <h2 className="py-2 text-sm font-semibold text-slate-800">
          Marker coverage
        </h2>
        {!coverage ? (
          <p className="pb-2 text-sm text-slate-500">
            Project path unreadable — no Flaitron PLAN to read.
          </p>
        ) : (
          <>
            <dl>
              <MetaRow label="Marked">
                <span className="font-mono text-xs">{coverage.marked}</span>
              </MetaRow>
              <MetaRow label="Denied">
                <span className="font-mono text-xs">{coverage.denied}</span>
              </MetaRow>
              <MetaRow label="Open queue">
                <span className="font-mono text-xs">{coverage.open}</span>
              </MetaRow>
              <MetaRow label="Unparsed">
                <span className="font-mono text-xs">{coverage.unparsed}</span>
              </MetaRow>
            </dl>
            <h3
              id="coverage-armed"
              className="pt-2 text-xs font-medium text-slate-700"
            >
              Armed queue — dispatch order
            </h3>
            {coverage.marked_tasks.length === 0 ? (
              <p className="py-1 text-sm text-slate-500">None armed.</p>
            ) : (
              <ol
                aria-labelledby="coverage-armed"
                className="list-decimal py-1 pl-6"
              >
                {coverage.marked_tasks.map((taskId) => (
                  <li key={taskId} className="font-mono text-xs text-slate-800">
                    {taskId}
                  </li>
                ))}
              </ol>
            )}
            {coverage.denials.length === 0 ? (
              <p className="py-2 text-sm text-slate-500">
                No denials — every open task carries the marker.
              </p>
            ) : (
              <>
                <p className="pt-2 text-xs text-slate-500">
                  In dispatch order — seed top-down. Green is the row with{" "}
                  <code className="font-mono">[unattended]</code> placed after{" "}
                  <code className="font-mono">[model]</code> and any
                  suggestion glyph.
                </p>
                {obvious.length > 0 ? (
                  <>
                    <h3
                      id="coverage-obvious"
                      className="pt-2 text-xs font-medium text-slate-700"
                    >
                      obvious ({obvious.length}) — mirrored predicate admits,
                      a marker suffices
                    </h3>
                    <DenialList denials={obvious} labelledBy="coverage-obvious" />
                    {needsJudgment.length > 0 ? (
                      <>
                        <h3
                          id="coverage-needs-judgment"
                          className="pt-2 text-xs font-medium text-slate-700"
                        >
                          needs judgment ({needsJudgment.length})
                        </h3>
                        <DenialList
                          denials={needsJudgment}
                          labelledBy="coverage-needs-judgment"
                        />
                      </>
                    ) : null}
                  </>
                ) : (
                  <DenialList denials={coverage.denials} />
                )}
              </>
            )}
            {coverage.unparsed_rows.length > 0 ? (
              <ul className="space-y-1 border-t border-slate-100 py-2">
                {coverage.unparsed_rows.map((row) => (
                  <li key={row.line} className="text-sm">
                    <span className="font-mono text-xs text-amber-700">
                      line {row.line}
                    </span>{" "}
                    <span className="text-slate-500">
                      unparsed — dropped by the task-line grammar:
                    </span>{" "}
                    <span className="font-mono text-xs text-slate-800">
                      {row.text}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <h2 className="py-2 text-sm font-semibold text-slate-800">
          Target tree
        </h2>
        {!tree ? (
          <p className="pb-2 text-sm text-slate-500">
            Project path unreadable — no target tree to read.
          </p>
        ) : !tree.readable ? (
          <p className="pb-2 text-sm text-amber-700">
            Unreadable — git could not report. A launch would refuse (exit 2).
          </p>
        ) : tree.clean ? (
          <p className="pb-2 text-sm text-slate-500">
            Clean — nothing would refuse a dispatch of{" "}
            <span className="font-mono text-xs text-slate-800">
              {tree.candidate ?? "(none eligible)"}
            </span>
            .
          </p>
        ) : (
          <>
            <p className="pb-1 text-sm text-amber-700">
              Dirty — {tree.blocking} change
              {tree.blocking === 1 ? "" : "s"} would refuse this dispatch (exit
              2).
            </p>
            <TreeBlockingDetail tree={tree} />
          </>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <h2 className="py-2 text-sm font-semibold text-slate-800">Last run</h2>
        <RunRecordDetail run={data.last_run} />
      </section>

      <section className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <h2 className="py-2 text-sm font-semibold text-slate-800">
          Run history
        </h2>
        <RunHistory runs={data.runs} counts={data.run_counts} />
      </section>

      <MarkdownPanel
        title="Last epic rollup"
        body={data.reports.last_rollup}
      />
    </div>
  );
}
