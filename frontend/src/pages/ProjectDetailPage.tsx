import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { Link, useParams } from "react-router-dom";

import { fetchProject } from "../api/client";
import type { ProjectDetail } from "../api/types";
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
          <p className="pb-2 text-sm text-slate-400">
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
          <p className="pb-2 text-sm text-slate-400">
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
            {coverage.denials.length === 0 ? (
              <p className="py-2 text-sm text-slate-400">
                No denials — every open task carries the marker.
              </p>
            ) : (
              <>
                <p className="pt-2 text-xs text-slate-400">
                  In dispatch order — seed top-down. Green is the row with{" "}
                  <code className="font-mono">[unattended]</code> placed after{" "}
                  <code className="font-mono">[model]</code> and any
                  suggestion glyph.
                </p>
                <ul className="space-y-2 py-2">
                  {coverage.denials.map((denial) => (
                    <li key={denial.task_id} className="text-sm">
                      <div>
                        {denial.line > 0 ? (
                          <span className="font-mono text-xs text-slate-400">
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
                      {denial.unparsed_blocker ? (
                        <div className="mt-1 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">
                          <span className="font-medium">
                            Says &ldquo;blocked&rdquo;, but no{" "}
                            <code className="font-mono">
                              Blocked by [[ID]]
                            </code>{" "}
                            clause parsed — check before seeding:
                          </span>{" "}
                          <span className="font-mono">
                            {denial.unparsed_blocker}
                          </span>
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
              </>
            )}
            {coverage.unparsed_rows.length > 0 ? (
              <ul className="space-y-1 border-t border-slate-100 py-2">
                {coverage.unparsed_rows.map((row) => (
                  <li key={row.line} className="text-sm">
                    <span className="font-mono text-xs text-amber-600">
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
          <p className="pb-2 text-sm text-slate-400">
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
