"use client"

import { memo, type ComponentPropsWithoutRef, type FC } from "react"
import {
  ComposerPrimitive,
  unstable_defaultDirectiveFormatter,
  unstable_useTriggerPopoverScopeContext,
  type Unstable_DirectiveFormatter,
  type Unstable_TriggerItem,
} from "@assistant-ui/react"
import { cn } from "cn"

import {
  ChevronLeftIcon,
  ChevronRightIcon,
  SparklesIcon,
} from "@/components/assistant-ui/elements/aui-icons"

type IconComponent = FC<{ className?: string }>

type DirectiveBehaviorProps = {
  /** Formatter used to serialize the selected item into composer text. */
  formatter?: Unstable_DirectiveFormatter | undefined
  /** Called after the directive text has been inserted into the composer. */
  onInserted?: ((item: Unstable_TriggerItem) => void) | undefined
}

type ActionBehaviorProps = {
  /** Formatter used to serialize the audit-trail chip (when `removeOnExecute` is false). */
  formatter?: Unstable_DirectiveFormatter | undefined
  /** Invoked with the selected item at the moment of selection. */
  onExecute: (item: Unstable_TriggerItem) => void
  /** If `true`, strip the trigger text from the composer after executing. @default false */
  removeOnExecute?: boolean | undefined
}

type ComposerTriggerPopoverBaseProps = Omit<
  ComponentPropsWithoutRef<typeof ComposerPrimitive.Unstable_TriggerPopover>,
  "children"
> & {
  /**
   * Maps icon keys to components. Items look up via `item.metadata?.icon`
   * (string); categories look up via their `id`.
   */
  iconMap?: Record<string, IconComponent>
  /** Fallback icon when no entry in `iconMap` matches. */
  fallbackIcon?: IconComponent
  /** Label shown on the back button. @default "Back" */
  backLabel?: string
  /** Label shown when no categories are available. @default "No items available" */
  emptyCategoriesLabel?: string
  /** Label shown when no items match. @default "No matching items" */
  emptyItemsLabel?: string
  /** Label shown while an async adapter is resolving items. @default "Loading…" */
  loadingLabel?: string
}

type ComposerTriggerPopoverProps = ComposerTriggerPopoverBaseProps &
  (
    | {
        /** Insert-directive behavior. */
        directive: DirectiveBehaviorProps
        action?: never
      }
    | {
        /** Action behavior. */
        action: ActionBehaviorProps
        directive?: never
      }
  )

function resolveIcon(
  iconKey: string | undefined,
  iconMap: Record<string, IconComponent> | undefined,
  fallback: IconComponent
): IconComponent {
  if (iconKey && iconMap?.[iconKey]) return iconMap[iconKey]!
  return fallback
}

// Rows match the Forge menus: 32px, 12px text, the item radius.
const row =
  "flex w-full min-h-8 cursor-default items-center gap-2 rounded-(--radius-item) px-2 py-1.5 text-start text-xs outline-none select-none hover:bg-accent data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground"

type CategoriesProps = {
  iconMap: Record<string, IconComponent> | undefined
  fallbackIcon: IconComponent
  emptyLabel: string
}

const Categories: FC<CategoriesProps> = ({
  iconMap,
  fallbackIcon,
  emptyLabel,
}) => (
  <ComposerPrimitive.Unstable_TriggerPopoverCategories>
    {(categories) => (
      <div
        data-slot="composer-trigger-popover-categories"
        className="flex flex-col"
      >
        {categories.map((cat) => {
          const Icon = resolveIcon(cat.id, iconMap, fallbackIcon)
          return (
            <ComposerPrimitive.Unstable_TriggerPopoverCategoryItem
              key={cat.id}
              categoryId={cat.id}
              className={cn(row, "justify-between")}
            >
              <span className="flex items-center gap-2">
                <Icon className="size-4 text-muted-foreground" />
                {cat.label}
              </span>
              <ChevronRightIcon className="size-3.5 text-muted-foreground" />
            </ComposerPrimitive.Unstable_TriggerPopoverCategoryItem>
          )
        })}
        {categories.length === 0 && (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            {emptyLabel}
          </div>
        )}
      </div>
    )}
  </ComposerPrimitive.Unstable_TriggerPopoverCategories>
)

type ItemsProps = {
  iconMap: Record<string, IconComponent> | undefined
  fallbackIcon: IconComponent
  backLabel: string
  emptyLabel: string
  loadingLabel: string
}

const Items: FC<ItemsProps> = ({
  iconMap,
  fallbackIcon,
  backLabel,
  emptyLabel,
  loadingLabel,
}) => {
  const { isLoading } = unstable_useTriggerPopoverScopeContext()
  return (
    <ComposerPrimitive.Unstable_TriggerPopoverItems>
      {(items) => (
        <div
          data-slot="composer-trigger-popover-items"
          className="flex flex-col"
        >
          <ComposerPrimitive.Unstable_TriggerPopoverBack
            className={cn(row, "gap-1 text-2xs text-muted-foreground")}
          >
            <ChevronLeftIcon className="size-3.5" />
            {backLabel}
          </ComposerPrimitive.Unstable_TriggerPopoverBack>

          {items.map((item, index) => {
            const iconKey =
              typeof item.metadata?.icon === "string"
                ? item.metadata.icon
                : undefined
            const Icon = resolveIcon(iconKey, iconMap, fallbackIcon)
            return (
              <ComposerPrimitive.Unstable_TriggerPopoverItem
                key={item.id}
                item={item}
                index={index}
                className={cn(row, "items-start")}
              >
                <Icon className="mt-px size-4 shrink-0 text-muted-foreground" />
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="font-mono text-foreground">
                    {item.label}
                  </span>
                  {item.description && (
                    <span className="text-2xs text-muted-foreground">
                      {item.description}
                    </span>
                  )}
                </span>
              </ComposerPrimitive.Unstable_TriggerPopoverItem>
            )
          })}
          {items.length === 0 && (
            <div className="px-2 py-1.5 text-xs text-muted-foreground">
              {isLoading ? loadingLabel : emptyLabel}
            </div>
          )}
        </div>
      )}
    </ComposerPrimitive.Unstable_TriggerPopoverItems>
  )
}

/**
 * Pre-built popover UI for a trigger-driven picker (mentions, slash commands, etc).
 * Pass exactly one of `directive` (inserts a chip) or `action` (fires a handler).
 */
const ComposerTriggerPopoverImpl: FC<ComposerTriggerPopoverProps> = ({
  iconMap,
  fallbackIcon = SparklesIcon,
  backLabel = "Back",
  emptyCategoriesLabel = "No items available",
  emptyItemsLabel = "No matching items",
  loadingLabel = "Loading…",
  className,
  directive,
  action,
  ...props
}) => {
  return (
    <ComposerPrimitive.Unstable_TriggerPopover
      data-slot="composer-trigger-popover"
      className={cn(
        "aui-composer-trigger-popover absolute start-0 bottom-full z-50 mb-2 max-h-72 w-72 overflow-y-auto rounded-(--radius-band) border border-border bg-popover p-1 text-popover-foreground shadow-lg",
        className
      )}
      {...props}
    >
      {directive ? (
        <ComposerPrimitive.Unstable_TriggerPopover.Directive
          formatter={directive.formatter ?? unstable_defaultDirectiveFormatter}
          onInserted={directive.onInserted}
        />
      ) : action ? (
        <ComposerPrimitive.Unstable_TriggerPopover.Action
          formatter={action.formatter ?? unstable_defaultDirectiveFormatter}
          onExecute={action.onExecute}
          removeOnExecute={action.removeOnExecute}
        />
      ) : null}
      <Categories
        iconMap={iconMap}
        fallbackIcon={fallbackIcon}
        emptyLabel={emptyCategoriesLabel}
      />
      <Items
        iconMap={iconMap}
        fallbackIcon={fallbackIcon}
        backLabel={backLabel}
        emptyLabel={emptyItemsLabel}
        loadingLabel={loadingLabel}
      />
    </ComposerPrimitive.Unstable_TriggerPopover>
  )
}
ComposerTriggerPopoverImpl.displayName = "ComposerTriggerPopover"

export const ComposerTriggerPopover = memo(
  ComposerTriggerPopoverImpl
) as FC<ComposerTriggerPopoverProps>
