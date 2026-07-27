import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  LifeBuoy,
  Flag,
  CheckCircle2,
  AlertCircle,
  FolderOpen,
  Download,
  ExternalLink,
  Loader2,
  Lock,
} from 'lucide-react'
import { Card, CardTitle, Btn, Toggle } from '../../components/ui'
import Modal from '../../components/Modal'
import { api, ApiError } from '../../api/client'

type CollectResult = Awaited<ReturnType<typeof api.collectDiagnostics>>

/**
 * Settings › About › "Report a Problem".
 *
 * Guided modal that calls the shared diagnostics collector (the same engine
 * behind `kirocrew doctor --bundle`): collects gateway + kiro-cli logs and
 * crash reports, scrubs secrets, zips them, and offers three deliveries —
 * reveal in Finder, download, or open a pre-filled GitHub issue.
 */
export default function ReportProblemCard() {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState('')
  const [includeLogs, setIncludeLogs] = useState(true)
  const [result, setResult] = useState<CollectResult | null>(null)
  const [error, setError] = useState('')

  const mut = useMutation({
    mutationFn: () => api.collectDiagnostics({ note, include_logs: includeLogs }),
    onMutate: () => {
      setError('')
      setResult(null)
    },
    onSuccess: (r) => setResult(r),
    onError: (e) =>
      setError(e instanceof ApiError ? e.message : 'Failed to collect diagnostics'),
  })

  const close = () => {
    if (mut.isPending) return
    setOpen(false)
    // Reset for the next open (after the close animation).
    window.setTimeout(() => {
      setResult(null)
      setError('')
      setNote('')
    }, 200)
  }

  return (
    <>
      <Card>
        <CardTitle>
          <LifeBuoy size={15} className="lucide-inline" /> Support
        </CardTitle>
        <div className="flex items-center justify-between gap-4 py-1.5">
          <span className="text-[13px] text-muted">
            Something not working? Send us a diagnostics report — logs are scrubbed of
            secrets first.
          </span>
          <Btn onClick={() => setOpen(true)}>
            <Flag size={13} className="lucide-inline" /> Report a Problem
          </Btn>
        </div>
      </Card>

      <Modal
        open={open}
        onClose={close}
        title="Report a Problem"
        maxWidth={560}
        footer={
          result ? (
            <Btn primary onClick={close}>
              Done
            </Btn>
          ) : (
            <>
              <Btn onClick={close} disabled={mut.isPending}>
                Cancel
              </Btn>
              <Btn primary disabled={mut.isPending} onClick={() => mut.mutate()}>
                {mut.isPending ? (
                  <>
                    <Loader2 size={13} className="lucide-inline animate-spin" /> Collecting…
                  </>
                ) : (
                  'Create report'
                )}
              </Btn>
            </>
          )
        }
      >
        {!result ? (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <span className="text-[13px] text-text font-medium">What happened?</span>
              <textarea
                aria-label="What happened?"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={'e.g. Every message fails with “process exited (rc=None)” after updating.'}
                rows={3}
                disabled={mut.isPending}
                className="text-sm px-2.5 py-2 rounded-md bg-bg border border-border resize-none"
              />
            </div>

            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="text-[13px] text-text font-medium">Include recent logs</div>
                <div className="text-[12px] text-muted">
                  Gateway + kiro-cli logs. Turn off to send crash reports only.
                </div>
              </div>
              <Toggle checked={includeLogs} onChange={setIncludeLogs} disabled={mut.isPending} />
            </div>

            <div
              className="text-[12px] text-muted rounded-md border border-border px-3 py-2 flex items-start gap-2"
              style={{ background: 'var(--ok-subtle)' }}
            >
              <Lock size={13} className="lucide-inline mt-0.5 shrink-0" />
              <span>
                Bearer tokens, session cookies, and AWS keys are automatically removed
                before the zip is created.
              </span>
            </div>

            {error && (
              <div className="text-[13px] text-danger flex items-center gap-1.5">
                <AlertCircle size={13} className="lucide-inline" /> {error}
              </div>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="text-[13px] text-ok flex items-center gap-1.5">
              <CheckCircle2 size={14} className="lucide-inline" /> Diagnostics ready —{' '}
              {result.total_redactions} secret(s) redacted across {result.included.length} file(s).
            </div>
            <div className="text-[12px] text-muted break-all">
              Saved to <code>{result.zip_path}</code>
            </div>
            <div className="flex flex-wrap gap-2">
              <Btn onClick={() => api.revealPath(result.zip_path)}>
                <FolderOpen size={13} className="lucide-inline" /> Show in Finder
              </Btn>
              <a href={result.download_url} download>
                <Btn>
                  <Download size={13} className="lucide-inline" /> Download zip
                </Btn>
              </a>
              <a href={result.github_issue_url} target="_blank" rel="noopener noreferrer">
                <Btn primary>
                  <ExternalLink size={13} className="lucide-inline" /> Open GitHub issue
                </Btn>
              </a>
            </div>
            <div className="text-[12px] text-muted">
              Opening the issue pre-fills the details — then drag the zip into it.
            </div>
          </div>
        )}
      </Modal>
    </>
  )
}
