import { useCallback } from 'react'
import type { useImeGuard } from '../../hooks/useImeGuard'
import { anotherEscapeOwnerIsOpen } from './escapeOwners'

/* Escape-to-stop for the composer (issue #22315). The CLI binds Esc to cancel
   the streaming turn; the dashboard had only the Stop button. This hook is the
   PRECEDENCE GATE for that binding: useComposerKeyDown calls it on a bare
   Escape, and it stops the running turn only when nothing else in the composer
   owns Escape first. It is deliberately NOT a page-wide shortcut — it lives on
   the textarea's own keydown, so a dialog, the command bar, a terminal or the
   transcript holding focus never reaches it. */

export function useComposerStopKey({ isRunning, onStop, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, ime }: {
  /** A turn is running on THIS composer's slot. */
  isRunning: boolean
  /** The Stop protocol the button runs (soft cancel, then force kill on a
   *  second press after the arm window). Absent on a surface with no stop. */
  onStop?: () => void
  /** Any trigger picker (/ @ $ path) is open: it owns Escape to close. */
  anyPickerOpenRef: React.RefObject<boolean>
  /** A dictation is recording or draining: voice.ts owns Escape to discard. */
  voiceRecording: boolean
  voiceDrainCancellable: boolean
  /** A pending tool approval is shown: its own Escape handling owns the key. */
  hasApproval: boolean
  /** The approval-mode nudge is up: Escape hides it for the session. */
  approvalNudgeActive: boolean
  /** The composer's IME guard. Used instead of the raw native flag because a
   *  WebKit keydown after `compositionend` arrives with `isComposing` already
   *  false and `keyCode === 229`; the guard's latch catches that, so an Escape
   *  that only cancels an IME candidate window never also stops the turn. */
  ime: Pick<ReturnType<typeof useImeGuard>, 'isComposing'>
}): (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean {
  return useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>): boolean => {
    // Leave Escape alone when there is no turn to stop, no stop on this
    // surface, or an IME composition owns the key (Escape cancels the
    // candidate window, never the turn). The composition check reads the
    // composer's guard, not the raw native flag, to also catch the
    // post-`compositionend` WebKit keydown (keyCode 229, isComposing false).
    //
    // These three are one compound condition rather than a standalone
    // composition-guard decline on its own line, on purpose: a bare
    // decline-return against the composition guard, in a handler whose event
    // type names a textarea, is exactly the shape the `ImeEnterClaimRatchet`
    // flags (a declined key the browser answers with a stray newline). This
    // hook's decline is an Escape, which inserts nothing, so it is not that
    // defect — folding the check into the compound keeps the hook clear of
    // the ratchet without the ratchet having to carve out an exception.
    if (ime.isComposing(e) || !isRunning || !onStop) return false
    // Precedence — Escape belongs to what owns it in the composer today, and
    // the turn is the LAST claimant. Each of these consumes Escape itself:
    //  - an open trigger picker closes (anyPickerOpenRef; some close without
    //    preventDefault, so the ref is the only reliable signal);
    //  - a recording or draining dictation discards (voice.ts);
    //  - a pending approval / its mode nudge dismisses (approval.ts).
    if (anyPickerOpenRef.current) return false
    if (voiceRecording || voiceDrainCancellable) return false
    if (hasApproval || approvalNudgeActive) return false
    // Any other surface that owns Escape takes it over the composer — the same
    // topmost-dismissible-surface rule voice.ts uses, asked through the shared
    // probe so the two gates cannot drift (escapeOwners.ts documents exactly
    // which surfaces count and why). Covers role="dialog" surfaces (minus the
    // read-only live-desktop opt-out) AND the non-dialog document-level owners
    // (embedded file preview, message-search bar) that close on Escape without
    // preventDefault — so a single Escape never both dismisses one of those
    // and also stops the turn.
    if (anotherEscapeOwnerIsOpen()) return false
    // The key is ours. But a HELD Escape must never escalate: after the first
    // soft cancel the slot is optimistically `soft_pending`, and a keydown more
    // than FORCE_KILL_ARMING_MS (400ms) later is read as a force kill by the
    // shared stop protocol — which discards the in-flight turn AND any queued
    // messages. OS key-repeat starts well inside that window, so holding Escape
    // a moment too long would hard-kill from one press. Swallow an auto-repeat
    // keydown (claim it so it reaches nothing else) without acting: escalation
    // then requires a deliberate release-and-press, exactly like the button.
    if (e.repeat) { e.preventDefault(); return true }
    // Stop the browser's default Escape (blur / clear) and run the same Stop
    // the button runs. A SECOND deliberate Escape after the arm window
    // force-kills, because onStop carries the press protocol (stopDebounce).
    e.preventDefault()
    onStop()
    return true
  }, [isRunning, onStop, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, ime])
}
