// The shell: rail | detail. Two columns on desktop, drill-down on mobile.
//
// The lists (pull requests / reviews) and the repo picker are stacked in the
// rail; everything to the right of it is the report you are reading. A third
// column spent a fixed slice of the window on a list you had already used to get
// where you are — and when no repo was selected it held nothing but an empty
// state pointing back at the rail.
//
// Reports are the widest content in the app (finding bodies, diffs, check
// tables), so the space belongs to them.
//
// On narrow viewports (<768px) the rail and detail CANNOT coexist: the rail's
// minimum width (280px) leaves the detail pane ~50px of content width — clipped
// text, overlapping buttons. So the rail collapses to a strip across the top,
// and expanding it takes the whole viewport (detail steps aside). Selecting a PR
// or run collapses the rail back, showing the report full-width.
import { useEffect } from 'react'
import { ScanSearch } from 'lucide-react'

import { useColumnResize, type CollapseConfig } from '../../hooks/useColumnResize'
import { useIsMobile } from '../../hooks/useIsMobile'
import EmptyState from './components/EmptyState'
import LeftRail from './components/LeftRail'
import PrReviewDetail from './components/PrReviewDetail'
import RunDetail from './components/RunDetail'
import { useSage } from './context'
import {
  COLLAPSED_RAIL_WIDTH, MAX_RAIL_WIDTH, MIN_RAIL_WIDTH,
  RAIL_COLLAPSED_KEY, RAIL_WIDTH_KEY, loadRailCollapsed, loadRailWidth,
} from './lib/layout'
import LearningView from './views/LearningView'
import SettingsView from './views/SettingsView'

import { i18nT } from '../../i18n/t'

// Module-level so the hook's memoised resolver isn't invalidated every render.
// `whenNarrow`: rail + detail cannot share a phone — the rail's minimum is
// MIN_RAIL_WIDTH and the detail carries findings and diffs, so the two become a
// drill-down instead. The expanded rail takes the WHOLE viewport, or the strip's
// expand control just leads back into the squeeze it escaped.
const RAIL_COLLAPSE: CollapseConfig = {
  width: COLLAPSED_RAIL_WIDTH,
  storageKey: RAIL_COLLAPSED_KEY,
  whenNarrow: true,
}

/** The 6px vertical drag handle between two columns. */
function Splitter({ handleProps, label }: {
  handleProps: ReturnType<typeof useColumnResize>['handleProps']
  label: string
}) {
  return (
    <div
      {...handleProps}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={i18nT('apps.codeReviewSage.workspace.drag_to_resize')}
      className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
      style={{ touchAction: 'none' }}
    />
  )
}

export default function Workspace() {
  const { mainView, activeRun, selectedPr } = useSage()
  const isMobile = useIsMobile()

  const rail = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH,
    RAIL_COLLAPSE, loadRailCollapsed,
  )

  // Collapsed while narrow: the rail lies ACROSS THE TOP rather than down the
  // side, so the pane below owns the full viewport width.
  const railBar = isMobile && rail.collapsed

  // Expanded while narrow: the rail IS the page, so the detail steps aside.
  const mobileRailOpen = isMobile && !rail.collapsed

  // Collapse on select — the third leg of the drill-down. Without it, picking a
  // review from the full-width rail changes nothing visible: the rail keeps the
  // viewport and the detail stays hidden, while the drag handle that would
  // collapse it is gone on touch.
  useEffect(() => {
    if (isMobile) rail.collapse()
  }, [selectedPr, activeRun, mainView, isMobile])

  return (
    // overflow-hidden so a mis-sized child can never grow the shell past the
    // viewport and push the rail's identity footer below the fold — each column
    // owns its own scrolling.
    <div className={`flex h-full overflow-hidden bg-bg text-text ${railBar ? 'flex-col' : ''}`}>
      {/* Collapsed strip: icon-only bar across the top on mobile. */}
      {rail.collapsed ? (
        <div
          className={railBar
            ? 'flex-shrink-0 flex flex-row items-center border-b border-border px-2 py-1.5 gap-1.5'
            : 'flex-shrink-0 flex flex-col items-center border-r border-border px-1 py-2 gap-2'}
          style={{ width: railBar ? undefined : COLLAPSED_RAIL_WIDTH }}
        >
          <button
            type="button"
            onClick={rail.expand}
            aria-label={i18nT('app.expand_sidebar')}
            className="p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer"
          >
            <ScanSearch size={16} aria-hidden="true" />
          </button>
        </div>
      ) : (
        <div
          style={{ width: mobileRailOpen ? '100%' : rail.width }}
          className="flex-shrink-0 min-h-0 flex"
        >
          <LeftRail />
        </div>
      )}

      {!isMobile && (
        <Splitter handleProps={rail.handleProps} label={i18nT('apps.codeReviewSage.workspace.resize_sidebar')} />
      )}

      {/* HIDDEN, never unmounted, while the full-width rail is up. Unmounting
          would discard in-progress state (chat, review selections). */}
      <main className={`flex-1 min-w-0 min-h-0 flex flex-col ${mobileRailOpen ? 'hidden' : 'flex'}`}>
        {mainView === 'reviews' ? (
          <>
            {selectedPr ? (
              <PrReviewDetail pr={selectedPr} />
            ) : activeRun ? (
              <RunDetail run={activeRun} />
            ) : (
              <EmptyState
                icon={ScanSearch}
                title={i18nT('apps.codeReviewSage.workspace.select_a_review_to_see_its_progress_and_report')}
                hint={i18nT('apps.codeReviewSage.workspace.start_a_new_one_several_can_run_at_once')}
              />
            )}
          </>
        ) : mainView === 'learning' ? (
          <LearningView />
        ) : (
          <SettingsView />
        )}
      </main>
    </div>
  )
}
