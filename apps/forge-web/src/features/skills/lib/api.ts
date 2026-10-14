import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

/*
 * An organization's skills vault, kept by the admin API (in MySQL): skills
 * (Agent Skills: a SKILL.md and the files beside it) that its agents' Skills
 * tools and its workflows' LLM agents load when a task calls for one. A
 * skill gets in four ways, all ending in the same `SkillBundle`: written
 * here, uploaded (read first, then imported), imported from GitHub (read
 * first, then imported; pulled again later), or written by the skill creator
 * in the background. Reading needs `organizations:read`; everything else
 * `skills:manage`.
 */

/** One of a skill's files, at its path in the skill's folder. */
export type SkillFile = {
  path: string
  /** Its text; base64 for a file that isn't UTF-8 text. */
  content: string
  encoding: "utf-8" | "base64"
}

/** A skill whole: what its SKILL.md says, and its files. */
export type SkillBundle = {
  /** Lowercase words joined by hyphens: what the model loads it by. */
  name: string
  /** What it does and when to use it: what decides when it's loaded. */
  description: string
  /** The SKILL.md body: the playbook the model reads when it loads it. */
  instructions: string
  license?: string | null
  compatibility?: string | null
  allowed_tools?: string | null
  metadata?: Record<string, unknown>
  /** Frontmatter fields the spec doesn't name, kept as they are. */
  extra?: Record<string, unknown>
  files: SkillFile[]
}

/** Where a skill came from. */
export type SkillSource =
  | { kind: "written" }
  | { kind: "upload"; folder?: string }
  | {
      kind: "github"
      repository: string
      ref: string
      commit: string
      path: string
      credential?: string
    }
  | { kind: "creator"; job?: string }

/** A skill as the vault lists it; `skill` only when it's read whole. */
export type SkillRecord = {
  id: string
  organization_id: string
  name: string
  description: string
  file_count: number
  size_bytes: number
  source: SkillSource
  created_at: string
  created_by: string
  updated_at: string
  updated_by: string
  skill?: SkillBundle
}

/** A skill found in an upload or a repository, not yet added. */
export type FoundSkill = {
  /** Its folder in what was read; empty for the top. */
  folder: string
  skill: SkillBundle
  source: SkillSource
  /** The organization's skill of the same name, which adding it would replace. */
  existing: string | null
}

/** What an upload or a repository holds. */
export type ReadResult = {
  skills: FoundSkill[]
  /** SKILL.md files that aren't skills as they are, and why. */
  problems: { path: string; message: string }[]
  /** Files that aren't any skill's. */
  skipped: string[]
}

export type GitHubRead = {
  /** The repository's URL, a folder's or a SKILL.md's, or owner/repo. */
  url: string
  /** The folder its skills are in; the URL's (or the whole repository) when empty. */
  path: string
  /** A branch, tag or commit; the URL's (or the default branch) when empty. */
  ref: string
  /** One of the organization's credentials that signs in to GitHub; empty for a public repository. */
  credential: string
}

export type ImportItem = {
  skill: SkillBundle
  source: SkillSource
  /** The organization's skill it replaces; null to add it. */
  replace: string | null
}

/** A skill the skill creator was asked to write. */
export type CreatorJob = {
  id: string
  organization_id: string
  brief: string
  /** The skill it reworks; null for a new one. */
  revises: string | null
  status: "running" | "done" | "failed"
  /** The skill it saved, once done. */
  skill_id: string | null
  /** For the person: what to check, connect or decide. */
  notes: string[]
  error: string | null
  model: string | null
  created_at: string
  created_by: string
  updated_at: string
}

export const organizationSkills = createNestedResource<
  SkillRecord,
  { organizationId: string },
  { create: SkillBundle; update: SkillBundle }
>({
  api,
  key: "organization-skills",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/skills`,
  label: "Skill",
  updateMethod: "put",
})

const skillsPath = (organizationId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/skills`
const jobsPath = (organizationId: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/skill-creator/jobs`

/** The organization's skills, by name (without their bodies). */
export function useOrganizationSkills(
  organizationId: string,
  { enabled = true } = {}
) {
  return organizationSkills
    .scope({ organizationId })
    .useList(undefined, { enabled })
}

/** One skill, whole. */
export function useSkill(organizationId: string, id: string | undefined) {
  return organizationSkills.scope({ organizationId }).useDetail(id)
}

/** A skill as a zip of its folder. */
export function downloadSkill(organizationId: string, id: string) {
  return api.get<Blob>(
    `${skillsPath(organizationId)}/${encodeURIComponent(id)}/download`,
    { responseType: "blob", silent: true }
  )
}

/**
 * Reads the skills in uploaded files without adding them: a folder of
 * skills, one skill's folder, a SKILL.md, or a zip. Each file goes with its
 * path in the folder picked (browsers send only its name).
 */
export function readUpload(
  organizationId: string,
  files: { file: File; path: string }[]
) {
  const form = new FormData()
  for (const { file } of files) form.append("files", file, file.name)
  form.append("paths", JSON.stringify(files.map(({ path }) => path)))
  return api.post<ReadResult, FormData>(
    `${skillsPath(organizationId)}/read`,
    form,
    {
      silent: true,
    }
  )
}

/** Reads the skills in a GitHub repository's folder without adding them. */
export function readGitHub(organizationId: string, body: GitHubRead) {
  return api.post<ReadResult, GitHubRead>(
    `${skillsPath(organizationId)}/github`,
    body,
    { silent: true }
  )
}

/** Adds the skills picked from an upload or a repository, together. */
export function useImportSkills(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (skills: ImportItem[]) =>
      api.post<SkillRecord[], { skills: ImportItem[] }>(
        `${skillsPath(organizationId)}/import`,
        { skills },
        { silent: true }
      ),
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: organizationSkills.scope({ organizationId }).keys.all,
      }),
  })
}

/** Pulls a skill imported from GitHub again, as its folder is now. */
export function usePullSkill(organizationId: string) {
  const client = useQueryClient()
  const scoped = organizationSkills.scope({ organizationId })
  return useMutation({
    mutationFn: (id: string) =>
      api.post<SkillRecord>(
        `${skillsPath(organizationId)}/${encodeURIComponent(id)}/pull`,
        undefined,
        { silent: true }
      ),
    onSuccess: (record) => {
      client.setQueryData(scoped.keys.detail(record.id), record)
      return client.invalidateQueries({ queryKey: scoped.keys.lists() })
    },
  })
}

const jobsKey = (organizationId: string) =>
  ["skill-creator-jobs", organizationId] as const

/**
 * The skill creator's jobs from the last week, newest first: polled while
 * one is running, and the skills refetched when one finishes.
 */
export function useCreatorJobs(organizationId: string) {
  const client = useQueryClient()
  return useQuery({
    queryKey: jobsKey(organizationId),
    queryFn: async ({ signal }) => {
      const jobs = await api.get<CreatorJob[]>(jobsPath(organizationId), {
        signal,
      })
      const before = client.getQueryData<CreatorJob[]>(jobsKey(organizationId))
      const finished = jobs.some(
        (job) =>
          job.status === "done" &&
          before?.find((b) => b.id === job.id)?.status === "running"
      )
      if (finished)
        void client.invalidateQueries({
          queryKey: organizationSkills.scope({ organizationId }).keys.all,
        })
      return jobs
    },
    refetchInterval: (query) =>
      query.state.data?.some((job) => job.status === "running") ? 3_000 : false,
  })
}

/** Has the skill creator write a skill (or rework one) in the background. */
export function useStartCreator(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: { brief: string; revises: string | null }) =>
      api.post<CreatorJob, typeof body>(jobsPath(organizationId), body, {
        silent: true,
      }),
    onSuccess: (job) => {
      client.setQueryData<CreatorJob[]>(jobsKey(organizationId), (jobs) => [
        job,
        ...(jobs ?? []).filter((j) => j.id !== job.id),
      ])
    },
  })
}

/** Takes a job off the list; one still running goes on. */
export function useDismissJob(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      api.delete<void>(`${jobsPath(organizationId)}/${encodeURIComponent(id)}`),
    meta: { errorTitle: "Couldn't dismiss it" },
    onSuccess: (_done, id) =>
      client.setQueryData<CreatorJob[]>(jobsKey(organizationId), (jobs) =>
        jobs?.filter((job) => job.id !== id)
      ),
  })
}
