import * as React from "react"
import { Link } from "@tanstack/react-router"
import {
  Folder01Icon,
  FolderAddIcon,
  FolderOpenIcon,
  FileEmpty02Icon,
  UserIcon,
} from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { ShellSidebarHeader, ShellSidebarTop } from "@/components/forge/shell"
import {
  NavItem,
  NavSectionHeading,
  SidebarSection,
  SubNav,
} from "@/components/forge/workspace-sidebar"
import { Button } from "@/components/ui/button"
import type {
  DocumentCollection,
  DocumentSummary,
  KnowledgeScope,
} from "../lib/api"
import {
  formatBytes,
  formatCount,
  KNOWLEDGE_ICON,
  totalsByState,
  UNFILED,
  type CollectionTree,
  type KnowledgePlace,
} from "../lib/knowledge"
import { IndexBar } from "./index-bar"

/**
 * A knowledge base's own sub nav, in place of the organization workspace's:
 * back to the organization's knowledge bases; where to look (all, indexing,
 * failed, yours); its collections as a tree, open down to the one you're in;
 * and a meter of what agents can search. Render it anywhere in the page: the
 * top block goes in the shell's pinned `ShellSidebarHeader`, the rest in
 * `ShellSidebarTop`, which scrolls on a short screen.
 */
export function KnowledgeNav({
  scope,
  place,
  summary,
  collections,
  tree,
  unfiled,
  canManage,
  onNewCollection,
}: {
  scope: KnowledgeScope
  /** Where the page is looking. */
  place: KnowledgePlace
  summary: DocumentSummary | undefined
  /** The collections; undefined while they load. */
  collections: DocumentCollection[] | undefined
  tree: CollectionTree
  /** How many documents are in no collection. */
  unfiled: number
  /** May create collections (`knowledge_bases:manage`). */
  canManage: boolean
  onNewCollection: () => void
}) {
  const states = totalsByState(summary)
  const total = summary?.totals.documents
  const unfinished = states.queued.documents + states.indexing.documents
  const link = (to: Partial<KnowledgePlace>) => (
    <Link
      to="/organizations/$organizationId/knowledge/$knowledgeBaseId"
      params={scope}
      search={(prev) => ({
        ...prev,
        shelf: to.shelf,
        collection: to.collection,
      })}
    />
  )
  const at = (shelf?: KnowledgePlace["shelf"], collection?: string) =>
    place.shelf === shelf && place.collection === collection

  return (
    <>
      <ShellSidebarHeader>
        <SidebarSection variant="primary">
          <NavItem
            icon="left"
            render={
              <Link
                to="/organizations/$organizationId"
                params={{ organizationId: scope.organizationId }}
                search={{ view: "knowledge" }}
              />
            }
          >
            <span className="truncate">Knowledge bases</span>
          </NavItem>
          <NavItem
            icon={KNOWLEDGE_ICON}
            active={at()}
            meta={total}
            render={link({})}
          >
            All documents
          </NavItem>
          <NavItem
            icon="loading"
            active={at("indexing")}
            meta={unfinished || undefined}
            render={link({ shelf: "indexing" })}
          >
            Indexing
          </NavItem>
          <NavItem
            icon="failed"
            active={at("failed")}
            meta={states.failed.documents || undefined}
            render={link({ shelf: "failed" })}
          >
            Failed
          </NavItem>
          <NavItem
            icon={UserIcon}
            active={at("mine")}
            render={link({ shelf: "mine" })}
          >
            Uploaded by you
          </NavItem>
        </SidebarSection>
      </ShellSidebarHeader>
      <ShellSidebarTop>
        <SidebarSection>
          <NavSectionHeading
            action={
              canManage ? (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="New collection"
                  onClick={onNewCollection}
                >
                  <Icon icon={FolderAddIcon} />
                </Button>
              ) : undefined
            }
          >
            Collections
          </NavSectionHeading>
          {collections !== undefined && (
            <CollectionBranch
              tree={tree}
              parent={null}
              open={
                new Set(
                  place.collection && place.collection !== UNFILED
                    ? tree.pathOf(place.collection).map((c) => c.id)
                    : []
                )
              }
              active={place.shelf ? undefined : place.collection}
              link={(id) => link({ collection: id })}
            />
          )}
          {collections !== undefined &&
            (collections.length > 0 || unfiled > 0) && (
              <NavItem
                icon={FileEmpty02Icon}
                active={at(undefined, UNFILED)}
                meta={unfiled}
                render={link({ collection: UNFILED })}
              >
                Unfiled
              </NavItem>
            )}
          {collections?.length === 0 && (
            <p className="px-2.5 py-1 text-xs text-subtle">
              No collections yet.
            </p>
          )}
        </SidebarSection>
        {summary && summary.totals.documents > 0 && (
          <SidebarSection variant="flush">
            <div className="rounded-(--radius-card) border px-3 py-2.5">
              <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="font-medium text-foreground">Index</span>
                <span className="text-subtle tabular-nums">
                  {formatCount(states.ready.documents)} of{" "}
                  {formatCount(summary.totals.documents)} ready
                </span>
              </div>
              <IndexBar totals={states} className="mt-2" />
              <p className="mt-2 text-2xs text-muted-foreground tabular-nums">
                {formatCount(summary.totals.chunks)} chunks ·{" "}
                {formatBytes(summary.totals.size_bytes)}
              </p>
            </div>
          </SidebarSection>
        )}
      </ShellSidebarTop>
    </>
  )
}

/**
 * One level of the collection tree: each collection, with the ones in it
 * shown for those on the way to where you are (and where you are).
 */
function CollectionBranch({
  tree,
  parent,
  open,
  active,
  link,
}: {
  tree: CollectionTree
  parent: string | null
  /** The collections from the top down to the one you're in. */
  open: Set<string>
  active: string | undefined
  link: (id: string) => React.ReactElement
}) {
  return tree.childrenOf(parent).map((collection) => {
    const expanded = open.has(collection.id)
    const children = tree.childrenOf(collection.id)
    return (
      <React.Fragment key={collection.id}>
        <NavItem
          icon={expanded && children.length > 0 ? FolderOpenIcon : Folder01Icon}
          size={parent ? "sm" : undefined}
          active={active === collection.id}
          meta={tree.totals(collection.id).documents}
          title={tree.pathName(collection.id)}
          render={link(collection.id)}
        >
          <span className="truncate">{collection.name}</span>
        </NavItem>
        {expanded && children.length > 0 && (
          <SubNav>
            <CollectionBranch
              tree={tree}
              parent={collection.id}
              open={open}
              active={active}
              link={link}
            />
          </SubNav>
        )}
      </React.Fragment>
    )
  })
}
