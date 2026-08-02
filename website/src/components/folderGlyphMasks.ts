/* Folder-glyph BACK-panel paint plumbing (data-only module — no UI copy).
 *
 * The sidebar's FolderGlyph draws its back panel by CLIPPING plain spans with
 * CSS `mask-image` data-URIs — deliberately NOT an inline <svg> icon: the icon
 * system is lucide-only (AUTOSDE `use-lucide-icons`), and this is shape
 * plumbing for a styled container, the same primitive family as the front
 * panel's bordered rounded rect.
 *
 * This lives in its own module because every string in it is SVG markup /
 * path data, never user-visible copy — the module is excluded by name in
 * eslint.i18n.config.js (same named-boundary idiom as `*.prompt.ts`), which
 * keeps ChatSidebar.tsx itself fully covered by the i18n literal gate. */

/** Outline weight shared by every panel edge so the glyph reads as one
 *  drawing: the front uses a plain CSS border; the back paints its outline
 *  through a stroked mask generated at the exact pixel size (below), so its
 *  width is uniform along the curves and matches the front. */
export const FOLDER_OUTLINE_PX = 0.75
export const FOLDER_OUTLINE = 'color-mix(in srgb, var(--text-strong) 70%, var(--accent))'
export const FOLDER_BACK_FILL = 'color-mix(in srgb, var(--text-strong) 22%, color-mix(in srgb, var(--accent) 14%, var(--bg-elevated)))'

/* BACK-panel silhouette — one continuous hand-authored path on a 56×42 grid
 * (insets match the front's 2% margins): tab (top y=1.5) easing down via an
 * S-bend into the raised body edge (y=5.5 ≈ 13% — ABOVE the front's 22%, so
 * the strip shows when closed), tight top-right corner completing at y=9
 * (≈21%) so it never gets cut mid-arc by the front. The body continues to
 * the bottom; its lower edges hide behind the opaque front (no doubles). */
const _FOLDER_BACK_PATH = 'M6 41L50 41C53.2 41 55 39.2 55 36L55 9C55 7 53.5 5.5 51.5 5.5L29.5 5.5C28.3 5.5 27.3 5.1 26.5 4.4L23.8 2.4C23 1.8 22 1.5 21 1.5L5.5 1.5C3 1.5 1 3.5 1 6L1 36C1 39.2 2.8 41 6 41z'

/** Wrap a path in a fill/stroke mask pair. Coordinates are PIXELS and the
 *  image carries explicit width/height (no viewBox, nothing stretches), so
 *  the stroked outline keeps constant width along every bend. Callers nest
 *  the line layer inside the fill layer, clipping the stroke's outer half —
 *  the visible outline is inside-aligned at exactly FOLDER_OUTLINE_PX. */
function _pathMasks(d: string, w: number, h: number): { fill: string; line: string } {
  const svg = (body: string) =>
    `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${h}'>${body}</svg>`)}")`
  return {
    fill: svg(`<path d='${d}' fill='white'/>`),
    // stroke straddles the path edge; the outer half is clipped by nesting
    line: svg(`<path d='${d}' fill='none' stroke='white' stroke-width='${2 * FOLDER_OUTLINE_PX}'/>`),
  }
}

const _folderMaskCache = new Map<string, { fill: string; line: string }>()

/** Fill + stroke mask pair for the back-panel silhouette at w×h px. */
export function folderBackMasks(w: number, h: number): { fill: string; line: string } {
  const key = `back:${w}:${h}`
  const hit = _folderMaskCache.get(key)
  if (hit) return hit
  const sx = w / 56
  const sy = h / 42
  let i = 0
  const d = _FOLDER_BACK_PATH.replace(/-?\d+(?:\.\d+)?/g, m => {
    const n = parseFloat(m)
    return String(Math.round((i++ % 2 === 0 ? n * sx : n * sy) * 100) / 100)
  })
  const masks = _pathMasks(d, w, h)
  _folderMaskCache.set(key, masks)
  return masks
}
