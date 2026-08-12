/**
 * AgentTemplateCreator component tests.
 * Ported from PR #2023 (@RohanK6) to strengthen the authoring dialog coverage.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  agentCreate: vi.fn(),
}))
const MockApiError = vi.hoisted(() => {
  return class MockApiError extends Error {
    readonly status: number
    readonly body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: MockApiError }))

import AgentTemplateCreator from '../components/AgentTemplateCreator'

const CATALOG = [
  { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
  { key: 'kiro-user/prepare-pr', name: 'prepare-pr', description: 'Ship a PR', source: 'kiro-user' },
]

function renderCreator(props: Partial<React.ComponentProps<typeof AgentTemplateCreator>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onCreated = props.onCreated ?? vi.fn()
  const onClose = props.onClose ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <AgentTemplateCreator
        open={props.open ?? true}
        onClose={onClose}
        onCreated={onCreated}
        modelOptions={props.modelOptions ?? [{ name: 'auto' }, { name: 'claude-opus' }]}
        existingNames={props.existingNames ?? ['kirocrew', 'taken']}
        mcpServerNames={props.mcpServerNames ?? ['probe-server']}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onCreated, onClose }
}

beforeEach(() => {
  mockApi.skills.mockReset()
  mockApi.agentCreate.mockReset()
  mockApi.skills.mockResolvedValue(CATALOG)
  mockApi.agentCreate.mockResolvedValue({ ok: true, name: 'my-agent', skills: [] })
})

const nameInput = () => screen.getByLabelText(/^name$/i)
const createBtn = () => screen.getByRole('button', { name: /^create$/i })

describe('AgentTemplateCreator', () => {
  it('disables Create until a valid name is entered', async () => {
    renderCreator()
    expect(createBtn()).toBeDisabled()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    expect(createBtn()).toBeEnabled()
  })

  it('rejects an invalid name client-side (slugify mismatch)', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'Bad Name' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/lowercase/i)).toBeInTheDocument()
  })

  it('refuses a duplicate name before spending a request', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'taken' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument()
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('submits the complete draft in one POST and reports the created name', async () => {
    const { onCreated } = renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/description/i), { target: { value: ' does things ' } })
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: 'You review.' } })
    // Map a skill from the catalog.
    fireEvent.click(await screen.findByRole('button', { name: /babysit/i }))
    fireEvent.click(createBtn())

    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalledTimes(1))
    const body = mockApi.agentCreate.mock.calls[0][0]
    expect(body).toMatchObject({
      name: 'my-agent',
      description: expect.stringContaining('does things'),
      prompt: 'You review.',
      skills: expect.arrayContaining(['babysit']),
    })
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('my-agent'))
  })

  it('toggling the shield marks a tool auto-approved (allowedTools)', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    // Type tool into input and submit via Enter
    const toolInput = screen.getByPlaceholderText(/tool/i)
    fireEvent.change(toolInput, { target: { value: 'fs_read' } })
    fireEvent.keyDown(toolInput, { key: 'Enter', code: 'Enter' })
    // Toggle auto-approve via title
    fireEvent.click(screen.getByTitle(/toggle auto-approve/i))
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    expect(mockApi.agentCreate.mock.calls[0][0]).toMatchObject({ allowedTools: expect.arrayContaining(['fs_read']) })
  })

  it('Cancel calls onClose', async () => {
    const { onClose } = renderCreator()
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onClose).toHaveBeenCalled()
  })

  it('shows a 409 name conflict from the server on the name field', async () => {
    mockApi.agentCreate.mockRejectedValue(
      new MockApiError(409, 'exists', JSON.stringify({
        error: "agent 'racer' already exists", code: 'name_exists', field: 'name',
      })),
    )
    renderCreator({ existingNames: [] })
    fireEvent.change(nameInput(), { target: { value: 'racer' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument()
  })

  it('offers probed MCP servers as suggestions', async () => {
    renderCreator({ mcpServerNames: ['probe-server'] })
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    // The suggestion chip reads "+ probe-server"
    const suggestion = await screen.findByRole('button', { name: /\+\s*probe-server/i })
    fireEvent.click(suggestion)
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    // MCP server added from suggestions
    const body = mockApi.agentCreate.mock.calls[0][0]
    expect(body.mcpServers).toHaveProperty('probe-server')
  })
})
