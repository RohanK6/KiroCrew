import { compareText } from '../i18n/format'
/**
 * Multi-select skill picker for the Hooks form.
 * Fetches installed skills from /api/skills and renders selected skills as
 * read-only chips. Add/remove actions live inside a single dropdown trigger
 * (one action per row).
 */
import { useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { Brain, ChevronDown, Minus, Plus } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { i18nT } from '../i18n/t'

interface CatalogSkill {
  key: string
  name: string
  description?: string
}

interface Props {
  /** Currently selected skill keys. */
  selected: string[]
  /** Called with the new full list on add/remove. */
  onChange: (skills: string[]) => void
}

export default function SkillsMultiSelect({ selected, onChange }: Props) {
  const btnRef = useRef<HTMLButtonElement>(null)

  const { data: catalog = [] } = useQuery<CatalogSkill[]>({
    queryKey: ['skills'],
    queryFn: async () => {
      const rows = await api.skills()
      return Array.isArray(rows) ? (rows as CatalogSkill[]).filter(s => s?.key) : []
    },
    staleTime: 60_000,
  })

  const byKey = useMemo(() => {
    const m = new Map<string, CatalogSkill>()
    for (const s of catalog) m.set(s.key, s)
    return m
  }, [catalog])

  const candidates = useMemo(
    () => catalog.filter(s => !selected.includes(s.key)).sort((a, b) => compareText(a.name, b.name)),
    [catalog, selected],
  )

  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered } =
    useFilteredDropdown(candidates)

  const add = (key: string) => {
    setOpen(false)
    onChange([...selected, key])
  }
  const remove = (key: string) => {
    setOpen(false)
    onChange(selected.filter(k => k !== key))
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 min-h-[32px]">
        {selected.map(key => {
          const skill = byKey.get(key)
          return (
            <span
              key={key}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[12px] font-mono bg-accent-subtle border border-accent/30 text-text"
              title={skill?.description || key}
            >
              <Brain className="lucide-inline" />
              {skill?.name || key.split('/').pop()}
            </span>
          )
        })}
        <div className="relative">
          <Btn
            ref={btnRef}
            className="flex items-center gap-1 px-2 py-0.5 text-[12px]"
            onClick={() => setOpen(!open)}
          >
            <Plus className="lucide-inline" /> {i18nT('components.skillsMultiSelect.add_skill')}
            <ChevronDown className="lucide-inline text-muted" />
          </Btn>
          {open && btnRef.current && createPortal(
            <div
              ref={dropdownRef}
              className="fixed z-50 bg-bg-elevated border border-border rounded-lg shadow-xl p-1 w-72 max-h-60 overflow-y-auto"
              style={{
                top: btnRef.current.getBoundingClientRect().bottom + 4,
                left: btnRef.current.getBoundingClientRect().left,
              }}
              onKeyDown={e => { if (e.key === 'Escape') { setOpen(false); btnRef.current?.focus() } }}
            >
              <Input
                ref={inputRef}
                placeholder={i18nT('components.skillsMultiSelect.filter_skills')}
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="mb-1 text-[12px]"
                autoFocus
              />
              {selected.length > 0 && (
                <>
                  <p className="text-[11px] text-muted px-2 pt-1 pb-0.5 font-medium uppercase tracking-wide">{i18nT('components.skillsMultiSelect.selected')}</p>
                  {selected.map(key => {
                    const skill = byKey.get(key)
                    return (
                      <button
                        key={key}
                        className="w-full text-left px-2 py-1.5 rounded text-[12px] hover:bg-danger-subtle transition-colors flex items-center gap-2"
                        onClick={() => remove(key)}
                        aria-label={i18nT('components.skillsMultiSelect.remove_skill', { name: skill?.name || key })}
                      >
                        <Minus className="lucide-inline shrink-0 text-danger" />
                        <span className="flex flex-col min-w-0">
                          <span className="font-medium truncate">{skill?.name || key.split('/').pop()}</span>
                          <span className="text-muted text-[11px] font-mono truncate">{key}</span>
                        </span>
                      </button>
                    )
                  })}
                  {filtered.length > 0 && <hr className="my-1 border-border" />}
                </>
              )}
              {filtered.length === 0 && selected.length === 0 && (
                <p className="text-[12px] text-muted px-2 py-1">{i18nT('components.skillsMultiSelect.no_matching_skills')}</p>
              )}
              {filtered.map(s => (
                <button
                  key={s.key}
                  className="w-full text-left px-2 py-1.5 rounded text-[12px] hover:bg-accent-subtle transition-colors flex items-center gap-2"
                  onClick={() => add(s.key)}
                >
                  <Brain className="lucide-inline shrink-0 text-accent" />
                  <span className="flex flex-col min-w-0">
                    <span className="font-medium truncate">{s.name}</span>
                    <span className="text-muted text-[11px] font-mono truncate">{s.key}</span>
                  </span>
                </button>
              ))}
            </div>,
            document.body,
          )}
        </div>
      </div>
      <p className="text-[11px] text-muted mt-1">{i18nT('components.skillsMultiSelect.skills_hint')}</p>
    </div>
  )
}
