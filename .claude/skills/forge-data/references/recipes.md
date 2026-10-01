# Recipes

Complete examples of common screens, combining the data layer with Forge UI.
They assume the app's clients live in `@/lib/api-instance` and its resources
in `@/lib/resources` (adjust to the app's actual modules). Each one
compiles against the real APIs.

## Contents

1. List with a filter and numbered pages
2. Detail page with a route loader
3. Create form with server-side field errors
4. Optimistic toggle
5. Delete with confirmation
6. Infinite list with auto-loading
7. Search as you type
8. Prefetch on hover

## 1. List with a filter and numbered pages

`keepPreviousData` keeps the current page on screen while the next one
loads, so the table doesn't flash to a skeleton on every page change.

```tsx
import { keepPreviousData } from "@tanstack/react-query"
import { useState } from "react"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { createResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

type Project = { id: number; name: string; status: "active" | "archived" }
type ProjectFilters = { status?: Project["status"]; page?: number }
type Page<T> = { data: T[]; total: number; pageSize: number }

export const projects = createResource<Project, { params: ProjectFilters; list: Page<Project> }>({
  api,
  path: "/projects",
  label: "project",
  mapItems: (page, map) => ({ ...page, data: map(page.data) }),
})

const statuses: { value: Project["status"]; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "archived", label: "Archived" },
]

export function ProjectsPage() {
  const [status, setStatus] = useState<Project["status"]>("active")
  const [page, setPage] = useState(1)
  const list = projects.useList({ status, page }, { placeholderData: keepPreviousData })

  if (list.isPending) return <Skeleton className="h-40 w-full" />
  if (list.isError) {
    return (
      <ErrorCallout
        title="Couldn't load projects"
        action={<Button variant="outline" size="sm" onClick={() => list.refetch()}>Retry</Button>}
      >
        {list.error.message}
      </ErrorCallout>
    )
  }

  const { data: rows, total, pageSize } = list.data
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return (
    <div className="flex flex-col gap-3">
      <Select
        items={statuses}
        value={status}
        onValueChange={(value) => {
          if (!value) return
          setStatus(value)
          setPage(1)
        }}
      >
        <SelectTrigger size="sm" aria-label="Status" className="w-36">
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {statuses.map((option) => (
            <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      {rows.length === 0 ? (
        <PageEmpty title="No projects" description="Projects you create appear here." />
      ) : (
        <Table>
          <TableHeader>
            <TableRow><TableHead>Name</TableHead><TableHead>Status</TableHead></TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((project) => (
              <TableRow key={project.id}>
                <TableCell>{project.name}</TableCell>
                <TableCell className="text-muted-foreground">{project.status}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</Button>
        <span>Page {page} of {pages}</span>
        <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</Button>
        {list.isFetching && <span className="text-subtle">Updating…</span>}
      </div>
    </div>
  )
}
```

## 2. Detail page with a route loader

Start the request in the router's loader so it runs in parallel with code
loading, then read it with `useSuspenseQuery` (no pending state to handle —
the router's pending/error UI covers it). Works with any router that has
loaders (React Router, TanStack Router).

```tsx
import { useSuspenseQuery } from "@tanstack/react-query"

import { tasks } from "@/lib/resources"
import { queryClient } from "@/lib/api-instance"

export function taskLoader({ params }: { params: { taskId?: string } }) {
  return queryClient.ensureQueryData(tasks.detailOptions(params.taskId!))
}

export function TaskDetail({ taskId }: { taskId: string }) {
  const { data: task } = useSuspenseQuery(tasks.detailOptions(taskId))
  return <h1 className="text-lg">{task.title}</h1>
}
```

Without a loader, `tasks.useDetail(taskId)` does the same with a pending
state.

## 3. Create form with server-side field errors

The form shows its own errors, so `meta: { silent: true }` keeps the global
toast out of it. 422s go under their fields; anything else goes in a callout
above the form. `onSuccess` runs after the list has refetched, so closing
the dialog there never shows stale data.

```tsx
import { useState } from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { tasks } from "@/lib/resources"

type ValidationBody = { errors?: Record<string, string[]> }

export function NewTaskDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [title, setTitle] = useState("")
  const create = tasks.useCreate({
    meta: { silent: true },
    onSuccess: () => {
      setTitle("")
      onOpenChange(false)
    },
  })

  const invalid = create.error?.status === 422
  const fieldErrors = invalid ? ((create.error?.data as ValidationBody | undefined)?.errors ?? {}) : {}

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>New task</DialogTitle></DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            create.mutate({ title, done: false })
          }}
        >
          <FieldGroup>
            {create.error && !invalid && (
              <ErrorCallout title="Couldn't create the task">{create.error.message}</ErrorCallout>
            )}
            <Field data-invalid={Boolean(fieldErrors.title)}>
              <FieldLabel htmlFor="task-title">Title</FieldLabel>
              <Input
                id="task-title"
                value={title}
                aria-invalid={Boolean(fieldErrors.title)}
                onChange={(event) => setTitle(event.target.value)}
              />
              {fieldErrors.title && <FieldError>{fieldErrors.title[0]}</FieldError>}
            </Field>
          </FieldGroup>
          <DialogFooter>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending && <Spinner data-icon="inline-start" />}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
```

(`Task` here is `{ id: string; title: string; done: boolean }`.)

## 4. Optimistic toggle

Toggles are the best fit for optimistic updates: the change is obvious,
reversible and cheap to roll back. On failure the checkbox flips back and
the global toast explains why.

```tsx
import { Checkbox } from "@/components/ui/checkbox"
import { tasks } from "@/lib/resources"

export function TaskDone({ task }: { task: { id: string; title: string; done: boolean } }) {
  const update = tasks.useUpdate({ optimistic: true })
  return (
    <label className="flex items-center gap-2 text-sm">
      <Checkbox
        checked={task.done}
        onCheckedChange={(checked) => update.mutate({ id: task.id, data: { done: checked === true } })}
      />
      {task.title}
    </label>
  )
}
```

Render `task` from `useList`/`useDetail` data (not local state) so the
optimistic edit and its rollback show up.

## 5. Delete with confirmation

```tsx
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { tasks } from "@/lib/resources"

export function DeleteTask({ id, title }: { id: string; title: string }) {
  const remove = tasks.useDelete()
  return (
    <Dialog>
      <DialogTrigger render={<Button variant="destructive" size="sm" />}>Delete</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete “{title}”?</DialogTitle>
          <DialogDescription>This can't be undone.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
          <Button variant="destructive" disabled={remove.isPending} onClick={() => remove.mutate(id)}>
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
```

The row disappears when the lists refetch (or at once with
`useDelete({ optimistic: true })`); the dialog unmounts with it.

## 6. Infinite list with auto-loading

The resource needs `pagination` (see resource.md §8). A sentinel element
triggers the next page as it scrolls into view; `LoadMore` stays as the
accessible, explicit fallback.

```tsx
import { useEffect, useRef } from "react"

import { LoadMore } from "@/components/forge/feedback"
import { createResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

type Post = { id: string; body: string }
type CursorPage = { items: Post[]; nextCursor: string | null }

export const posts = createResource<Post, { list: CursorPage }>({
  api,
  path: "/posts",
  mapItems: (page, map) => ({ ...page, items: map(page.items) }),
  pagination: { param: "cursor", initialPageParam: null, getNextPageParam: (last) => last.nextCursor },
})

export function PostFeed() {
  const feed = posts.useInfiniteList(undefined, {
    select: (data) => data.pages.flatMap((page) => page.items),
  })
  const sentinel = useRef<HTMLDivElement>(null)
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = feed

  useEffect(() => {
    const node = sentinel.current
    if (!node || !hasNextPage) return
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && !isFetchingNextPage) void fetchNextPage()
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage])

  return (
    <div>
      {feed.data?.map((post) => <p key={post.id} className="py-2 text-sm">{post.body}</p>)}
      <div ref={sentinel} />
      {hasNextPage && (
        <LoadMore disabled={isFetchingNextPage} onClick={() => void fetchNextPage()} />
      )}
    </div>
  )
}
```

## 7. Search as you type

Debounce the input, put the debounced value in the list params (it becomes
part of the query key, so each search is cached), and keep the previous
results visible while the next ones load.

```tsx
import { keepPreviousData } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { SearchField } from "@/components/forge/toolbar"
import { tasks } from "@/lib/resources"

function useDebounced<T>(value: T, ms = 250) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return debounced
}

export function TaskSearch() {
  const [q, setQ] = useState("")
  const query = useDebounced(q.trim())
  const results = tasks.useList(query ? { q: query } : undefined, {
    placeholderData: keepPreviousData,
  })
  return (
    <div>
      <SearchField value={q} onChange={(event) => setQ(event.target.value)} />
      <ul className={results.isPlaceholderData ? "opacity-60" : undefined}>
        {results.data?.map((task) => <li key={task.id}>{task.title}</li>)}
      </ul>
    </div>
  )
}
```

## 8. Prefetch on hover

Warm the detail cache when the user shows intent, so opening it is instant.
`prefetchQuery` does nothing if the data is already fresh.

```tsx
import { useQueryClient } from "@tanstack/react-query"

import { tasks } from "@/lib/resources"

export function TaskLink({ id, title, onOpen }: { id: string; title: string; onOpen: () => void }) {
  const queryClient = useQueryClient()
  return (
    <button
      type="button"
      onMouseEnter={() => void queryClient.prefetchQuery(tasks.detailOptions(id))}
      onFocus={() => void queryClient.prefetchQuery(tasks.detailOptions(id))}
      onClick={onOpen}
    >
      {title}
    </button>
  )
}
```
