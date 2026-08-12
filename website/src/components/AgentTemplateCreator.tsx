import { useState, useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Brain, Plus, Shield, X } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter,
} from './ui/dialog'
import SimpleSelect from './SimpleSelect'
import { i18nT } from '../i18n/t'

interface AgentDetail {
  name: string
  description?: string
  model?: string
  prompt?: string
  skills?: string[]
  tools?: string[]
  allowedTools?: string[]
  mcpServers?: Record<string, { args?: string[] }>
  toolsSettings?: { execute_bash?: { deniedCommands?: string[] } }
}

interface Props {
  open: boolean
  onClose: () => void
  onCreated: (name: string) => void
  modelOptions: { name: string; label?: string }[]
  existingNames: string[]
  mcpServerNames?: string[]
  editTarget?: AgentDetail | null
  cloneMode?: boolean
}

/** Sanitize a name to a valid agent template slug. */
function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '')
}

const MAX_SUGGESTIONS = 12

export default function AgentTemplateCreator({
  open, onClose, onCreated, modelOptions, existingNames, mcpServerNames = [], editTarget, cloneMode,
}: Props) {
  const isEdit = !!editTarget && !cloneMode
  const isClone = !!editTarget && !!cloneMode

  // Form state
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [model, setModel] = useState('auto')
  const [prompt, setPrompt] = useState('')
  const [tools, setTools] = useState<string[]>([])
  const [allowedTools, setAllowedTools] = useState<string[]>([])
  const [mcpServers, setMcpServers] = useState<{ name: string; args: string }[]>([])
  const [skills, setSkills] = useState<string[]>([])

  // Tool input
  const [toolInput, setToolInput] = useState('')
  const [mcpName, setMcpName] = useState('')
  const [mcpArgs, setMcpArgs] = useState('')

  // Suggestions overflow
  const [showAllSuggestions, setShowAllSuggestions] = useState(false)

  // Error/validation
  const [nameError, setNameError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)

  // Skill catalog
  const { data: skillCatalog = [] } = useQuery({
    queryKey: ['skills-catalog'],
    queryFn: async () => {
      const skills = await api.skills()
      return Array.isArray(skills) ? skills.map((s: { key: string; name: string }) => s.key) : []
    },
    enabled: open,
  })

  // Seed form when editing/cloning
  useEffect(() => {
    if (!open) return
    if (editTarget) {
      setName(isClone ? '' : editTarget.name)
      setDescription(editTarget.description || '')
      setModel(editTarget.model || 'auto')
      setPrompt(editTarget.prompt || '')
      setSkills(editTarget.skills || [])
      setTools(editTarget.tools || [])
      setAllowedTools(editTarget.allowedTools || [])
      const servers = editTarget.mcpServers
        ? Object.entries(editTarget.mcpServers).map(([n, v]) => ({
            name: n,
            args: (v?.args || []).join(' '),
          }))
        : []
      setMcpServers(servers)
    } else {
      setName('')
      setDescription('')
      setModel('auto')
      setPrompt('')
      setSkills([])
      setTools([])
      setAllowedTools([])
      setMcpServers([])
    }
    setToolInput('')
    setMcpName('')
    setMcpArgs('')
    setNameError('')
    setShowAllSuggestions(false)
  }, [open, editTarget, isClone])

  // MCP server suggestions
  const mcpSuggestions = useMemo(() => {
    const used = new Set(mcpServers.map(s => s.name))
    return mcpServerNames.filter(n => !used.has(n))
  }, [mcpServerNames, mcpServers])

  const visibleSuggestions = showAllSuggestions
    ? mcpSuggestions
    : mcpSuggestions.slice(0, MAX_SUGGESTIONS)
  const overflowCount = mcpSuggestions.length - MAX_SUGGESTIONS

  // Validate name
  const validateName = (v: string): string => {
    if (!v.trim()) return i18nT('components.agentTemplateCreator.name_required')
    const slug = slugify(v)
    if (slug !== v) return i18nT('components.agentTemplateCreator.name_must_be_lowercase')
    if (!isEdit && existingNames.includes(v)) return i18nT('components.agentTemplateCreator.name_already_exists')
    return ''
  }

  // Submit
  const createMut = useMutation({
    mutationFn: (payload: object) => api.agentCreate(payload),
    onSuccess: () => { onCreated(name); onClose() },
  })

  const updateMut = useMutation({
    mutationFn: (payload: object) => api.agentUpdate(editTarget!.name, payload),
    onSuccess: () => { onCreated(editTarget!.name); onClose() },
  })

  const handleSubmit = () => {
    const err = validateName(name)
    if (err && !isEdit) {
      setNameError(err)
      nameRef.current?.focus()
      return
    }

    // Build MCP servers object
    const mcpObj: Record<string, { args?: string[] }> = {}
    for (const s of mcpServers) {
      const args = s.args.trim() ? s.args.trim().split(/\s+/) : undefined
      mcpObj[s.name] = args ? { args } : {}
    }

    const payload: Record<string, unknown> = {
      name: isEdit ? editTarget!.name : name,
      description,
      model: model === 'auto' ? '' : model,
      prompt,
      skills,
      tools: [...tools],
      allowedTools: [...allowedTools],
      mcpServers: Object.keys(mcpObj).length > 0 ? mcpObj : undefined,
    }

    if (isEdit) {
      updateMut.mutate(payload)
    } else {
      createMut.mutate(payload)
    }
  }

  const isPending = createMut.isPending || updateMut.isPending
  const mutError = createMut.error || updateMut.error

  const addTool = () => {
    const t = toolInput.trim()
    if (!t || tools.includes(t)) return
    setTools([...tools, t])
    setToolInput('')
  }

  const removeTool = (t: string) => setTools(tools.filter(x => x !== t))

  const toggleApproved = (t: string) => {
    if (allowedTools.includes(t)) {
      setAllowedTools(allowedTools.filter(x => x !== t))
    } else {
      setAllowedTools([...allowedTools, t])
    }
  }



  const addMcpServer = (serverName?: string) => {
    const n = (serverName || mcpName).trim()
    if (!n || mcpServers.some(s => s.name === n)) return
    setMcpServers([...mcpServers, { name: n, args: serverName ? '' : mcpArgs }])
    setMcpName('')
    setMcpArgs('')
  }

  const removeMcpServer = (n: string) => setMcpServers(mcpServers.filter(s => s.name !== n))

  const title = isEdit
    ? i18nT('components.agentTemplateCreator.edit_agent_template')
    : isClone
      ? i18nT('components.agentTemplateCreator.clone_agent_template')
      : i18nT('components.agentTemplateCreator.create_agent_template')

  return (
    <Dialog open={open} onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent maxWidth={720}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {/* Name */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.name')}
            </label>
            <Input
              ref={nameRef}
              value={name}
              onChange={e => { setName(e.target.value); setNameError('') }}
              placeholder="my-agent"
              disabled={isEdit}
              className="w-full font-mono text-[13px]"
              aria-invalid={!!nameError}
            />
            {nameError && <p className="m-0 text-[11px] text-danger">{nameError}</p>}
          </fieldset>

          {/* Description */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.description')}
            </label>
            <Input
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder={i18nT('components.agentTemplateCreator.description_placeholder')}
              className="w-full text-[13px]"
            />
          </fieldset>

          {/* Model */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.model')}
            </label>
            <SimpleSelect
              options={['auto', ...modelOptions.map(m => m.name)]}
              value={model}
              onChange={setModel}
              aria-label={i18nT('components.agentTemplateCreator.model')}
              style={{ width: '100%' }}
            />
          </fieldset>

          {/* Skills */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.skills')}
            </label>
            <div className="flex flex-wrap gap-1.5">
              {skillCatalog.map(sk => (
                <Btn
                  key={sk}
                  type="button"
                  className={`px-2 py-1 text-[12px] font-mono rounded-full border ${
                    skills.includes(sk)
                      ? 'bg-accent/15 border-accent/40 text-accent'
                      : 'bg-bg-elevated border-border text-muted hover:text-text'
                  }`}
                  onClick={() => {
                    setSkills(skills.includes(sk) ? skills.filter(s => s !== sk) : [...skills, sk])
                  }}
                >
                  <Brain className="lucide-inline" /> {sk}
                </Btn>
              ))}
              {skillCatalog.length === 0 && (
                <span className="text-[12px] text-muted">{i18nT('components.agentTemplateCreator.no_skills_available')}</span>
              )}
            </div>
          </fieldset>

          {/* Tools */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.tools')}
            </label>
            <div className="flex gap-2">
              <Input
                value={toolInput}
                onChange={e => setToolInput(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.tool_name_placeholder')}
                className="flex-1 text-[13px] font-mono"
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTool() } }}
              />
              <Btn type="button" onClick={addTool}><Plus className="lucide-inline" /></Btn>
            </div>
            {tools.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {tools.map(t => (
                  <span key={t} className="group inline-flex items-center gap-1 px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-text">
                    {t}
                    <button
                      type="button"
                      className={`ml-0.5 p-0.5 rounded transition-colors ${allowedTools.includes(t) ? 'text-ok' : 'text-muted hover:text-ok'}`}
                      onClick={() => toggleApproved(t)}
                      title={i18nT('components.agentTemplateCreator.toggle_auto_approve')}
                    >
                      <Shield size={12} />
                    </button>
                    <button type="button" className="text-muted hover:text-danger" onClick={() => removeTool(t)}>
                      <X size={12} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {tools.length > 0 && (
              <p className="m-0 text-[11px] text-muted flex items-center gap-1">
                <Shield size={11} className="text-ok" />
                {i18nT('components.agentTemplateCreator.shield_helper')}
              </p>
            )}
          </fieldset>

          {/* MCP Servers */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.mcp_servers')}
            </label>
            {mcpServers.map(s => (
              <div key={s.name} className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5">
                <span className="text-[12px] font-mono text-aim flex-1 min-w-0 truncate">{s.name}</span>
                {s.args && <span className="text-[11px] text-muted font-mono truncate max-w-[140px]">{s.args}</span>}
                <button type="button" className="text-muted hover:text-danger shrink-0" onClick={() => removeMcpServer(s.name)}>
                  <X size={14} />
                </button>
              </div>
            ))}
            <div className="flex gap-2">
              <Input
                value={mcpName}
                onChange={e => setMcpName(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.server_name_placeholder')}
                className="flex-1 text-[13px] font-mono"
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addMcpServer() } }}
              />
              <Input
                value={mcpArgs}
                onChange={e => setMcpArgs(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.args_placeholder')}
                className="w-[140px] text-[12px] font-mono"
              />
              <Btn type="button" onClick={() => addMcpServer()}><Plus className="lucide-inline" /></Btn>
            </div>
            {mcpSuggestions.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {visibleSuggestions.map(s => (
                  <Btn
                    key={s}
                    type="button"
                    className="px-2 py-0.5 text-[11px] font-mono text-aim bg-aim/10 border border-aim/20 rounded-full"
                    onClick={() => addMcpServer(s)}
                  >
                    + {s}
                  </Btn>
                ))}
                {!showAllSuggestions && overflowCount > 0 && (
                  <Btn
                    type="button"
                    className="px-2 py-0.5 text-[11px] text-muted border border-border rounded-full"
                    onClick={() => setShowAllSuggestions(true)}
                  >
                    +{overflowCount} {i18nT('components.agentTemplateCreator.more')}
                  </Btn>
                )}
              </div>
            )}
          </fieldset>

          {/* System Prompt */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.system_prompt')}
            </label>
            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              placeholder={i18nT('components.agentTemplateCreator.prompt_placeholder')}
              className="w-full min-h-[100px] rounded-md border border-border bg-bg-elevated px-3 py-2 text-[12.5px] font-mono text-text placeholder:text-muted resize-y outline-none focus:border-accent"
              rows={5}
            />
          </fieldset>

          {/* Denied Commands */}
          {/* Mutation error */}
          {mutError && (
            <p className="m-0 text-[12px] text-danger rounded-md border border-danger/30 bg-danger/10 px-3 py-2">
              {mutError instanceof Error ? mutError.message : String(mutError)}
            </p>
          )}
        </DialogBody>

        <DialogFooter>
          <Btn type="button" onClick={onClose}>
            {i18nT('components.agentTemplateCreator.cancel')}
          </Btn>
          <Btn type="button" className="bg-accent text-white hover:bg-accent/90" onClick={handleSubmit} disabled={isPending}>
            {isPending
              ? i18nT('components.agentTemplateCreator.saving')
              : isEdit
                ? i18nT('components.agentTemplateCreator.save_changes')
                : i18nT('components.agentTemplateCreator.create')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
