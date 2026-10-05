import * as React from "react"
import { HugeiconsIcon, type IconSvgElement } from "@hugeicons/react"
import {
  Add01Icon,
  AlertCircleIcon as AlertCircleGlyph,
  ArchiveIcon as ArchiveGlyph,
  ArrowDown01Icon,
  ArrowDown02Icon,
  ArrowLeft01Icon,
  ArrowRight01Icon,
  ArrowUp02Icon,
  AudioWave01Icon,
  BotIcon as BotGlyph,
  BrainIcon as BrainGlyph,
  Call02Icon,
  Cancel01Icon,
  CancelCircleIcon,
  CodeIcon,
  Copy01Icon,
  Delete02Icon,
  Download01Icon,
  File01Icon,
  FileAttachmentIcon,
  HistoryIcon as HistoryGlyph,
  Image01Icon,
  ImageNotFound01Icon,
  Loading03Icon,
  Mic01Icon,
  MoreHorizontalIcon as MoreHorizontalGlyph,
  MusicNote01Icon,
  PencilEdit02Icon,
  RefreshIcon,
  Search01Icon,
  SecurityWarningIcon,
  SparklesIcon as SparklesGlyph,
  StopIcon,
  ThumbsDownIcon as ThumbsDownGlyph,
  ThumbsUpIcon as ThumbsUpGlyph,
  Tick02Icon,
  Video01Icon,
} from "@hugeicons/core-free-icons"

/*
 * The assistant-ui elements are written against lucide-react. These
 * stand-ins keep their names but draw Hugeicons at the Forge stroke (1.6),
 * reusing the Forge vocabulary's glyph wherever the meaning overlaps
 * (check, copy, refresh, close, …), so the elements only change by one
 * import line and upstream diffs stay readable.
 */

type IconProps = Omit<React.ComponentProps<typeof HugeiconsIcon>, "icon">

function glyph(icon: IconSvgElement, displayName: string) {
  function Glyph(props: IconProps) {
    return <HugeiconsIcon icon={icon} strokeWidth={1.6} {...props} />
  }
  Glyph.displayName = displayName
  return Glyph
}

export const AlertCircleIcon = glyph(AlertCircleGlyph, "AlertCircleIcon")
export const ArchiveIcon = glyph(ArchiveGlyph, "ArchiveIcon")
export const ArrowDownIcon = glyph(ArrowDown02Icon, "ArrowDownIcon")
export const ArrowUpIcon = glyph(ArrowUp02Icon, "ArrowUpIcon")
export const AudioLinesIcon = glyph(AudioWave01Icon, "AudioLinesIcon")
export const BotIcon = glyph(BotGlyph, "BotIcon")
export const BracesIcon = glyph(CodeIcon, "BracesIcon")
export const BrainIcon = glyph(BrainGlyph, "BrainIcon")
export const CheckIcon = glyph(Tick02Icon, "CheckIcon")
export const ChevronDownIcon = glyph(ArrowDown01Icon, "ChevronDownIcon")
export const ChevronLeftIcon = glyph(ArrowLeft01Icon, "ChevronLeftIcon")
export const ChevronRightIcon = glyph(ArrowRight01Icon, "ChevronRightIcon")
export const CopyIcon = glyph(Copy01Icon, "CopyIcon")
export const DownloadIcon = glyph(Download01Icon, "DownloadIcon")
export const FileIcon = glyph(File01Icon, "FileIcon")
export const FileText = glyph(FileAttachmentIcon, "FileText")
export const FileTextIcon = glyph(FileAttachmentIcon, "FileTextIcon")
export const HistoryIcon = glyph(HistoryGlyph, "HistoryIcon")
export const ImageIcon = glyph(Image01Icon, "ImageIcon")
export const ImageOffIcon = glyph(ImageNotFound01Icon, "ImageOffIcon")
export const Loader2Icon = glyph(Loading03Icon, "Loader2Icon")
export const LoaderIcon = glyph(Loading03Icon, "LoaderIcon")
export const MicIcon = glyph(Mic01Icon, "MicIcon")
export const MoreHorizontalIcon = glyph(
  MoreHorizontalGlyph,
  "MoreHorizontalIcon"
)
export const MusicIcon = glyph(MusicNote01Icon, "MusicIcon")
export const PencilIcon = glyph(PencilEdit02Icon, "PencilIcon")
export const PhoneIcon = glyph(Call02Icon, "PhoneIcon")
export const PlusIcon = glyph(Add01Icon, "PlusIcon")
export const RefreshCwIcon = glyph(RefreshIcon, "RefreshCwIcon")
export const SearchIcon = glyph(Search01Icon, "SearchIcon")
export const ShieldAlertIcon = glyph(SecurityWarningIcon, "ShieldAlertIcon")
export const SparklesIcon = glyph(SparklesGlyph, "SparklesIcon")
export const SquareIcon = glyph(StopIcon, "SquareIcon")
export const ThumbsDownIcon = glyph(ThumbsDownGlyph, "ThumbsDownIcon")
export const ThumbsUpIcon = glyph(ThumbsUpGlyph, "ThumbsUpIcon")
export const TrashIcon = glyph(Delete02Icon, "TrashIcon")
export const VideoIcon = glyph(Video01Icon, "VideoIcon")
export const XCircleIcon = glyph(CancelCircleIcon, "XCircleIcon")
export const XIcon = glyph(Cancel01Icon, "XIcon")
