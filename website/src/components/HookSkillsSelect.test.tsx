import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import HookSkillsSelect from './HookSkillsSelect'
import { api } from '../api/client'

vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, skills: vi.fn() } }
})

const mockSkills = [
  { key: 'kirocrew-dev/prepare-pr', name: 'prepare-pr', description: 'PR workflow' },
  { key: 'dev-fleet/pod-e2e', name: 'pod-e2e', description: 'E2E tests' },
  { key: 'widgets/theme-pack', name: 'theme-pack', description: 'Theme authoring' },
]

describe('HookSkillsSelect', () => {
  beforeEach(() => {
    vi.mocked(api.skills).mockResolvedValue(mockSkills as never)
  })

  it('renders the add-skill button when selection is empty', () => {
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument()
  })

  it('renders selected skills as read-only chips', async () => {
    renderWithProviders(
      <HookSkillsSelect selected={['kirocrew-dev/prepare-pr', 'dev-fleet/pod-e2e']} onChange={vi.fn()} />,
    )
    await waitFor(() => {
      expect(screen.getByText(/prepare-pr/)).toBeInTheDocument()
      expect(screen.getByText(/pod-e2e/)).toBeInTheDocument()
    })
  })

  it('does not render inline remove buttons (actions live in dropdown)', async () => {
    renderWithProviders(
      <HookSkillsSelect selected={['kirocrew-dev/prepare-pr']} onChange={vi.fn()} />,
    )
    await waitFor(() => expect(screen.getByText(/prepare-pr/)).toBeInTheDocument())
    // No inline remove buttons — only the Add button should exist in the row
    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBe(1) // Only "Add skill" button
  })

  it('renders skill chips with fallback name when catalog has not loaded', () => {
    vi.mocked(api.skills).mockResolvedValue([] as never)
    renderWithProviders(
      <HookSkillsSelect selected={['unknown/skill-name']} onChange={vi.fn()} />,
    )
    // Fallback: key.split('/').pop()
    expect(screen.getByText(/skill-name/)).toBeInTheDocument()
  })

  it('handles api.skills returning non-array gracefully', async () => {
    vi.mocked(api.skills).mockResolvedValue(null as never)
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument()
  })

  it('handles api.skills returning items without key', async () => {
    vi.mocked(api.skills).mockResolvedValue([
      { key: '', name: 'empty' },
      { key: 'valid/skill', name: 'valid' },
    ] as never)
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: /add skill/i })).toBeInTheDocument()
  })

  it('shows hint text below the chips', () => {
    renderWithProviders(<HookSkillsSelect selected={[]} onChange={vi.fn()} />)
    const hint = document.querySelector('p.text-muted')
    expect(hint).not.toBeNull()
  })
})
