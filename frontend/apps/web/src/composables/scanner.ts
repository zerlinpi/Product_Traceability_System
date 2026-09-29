/**
 * USB/HID scan-gun support.
 *
 * A scan gun in keyboard mode "types" the code and presses Enter (main or
 * numpad — both report `event.key === 'Enter'`). Field pages must therefore:
 *
 * 1. keep the scan box focused, even after a dialog closes or the page
 *    transition finishes — `focusScanInput` retries on the next frame and
 *    again at 60 ms / 180 ms, which is what made focus reliable on the old UI;
 * 2. catch keystrokes that arrive while focus is somewhere neutral (a button,
 *    the page body) and route them into the scan box instead of losing them;
 * 3. never steal keystrokes from a real form field or an open dialog.
 */
import type { MaybeRefOrGetter } from 'vue'

/** True when an Element Plus dialog / message box or a Fa modal is open. */
export function isAnyDialogOpen() {
  const overlays = Array.from(document.querySelectorAll<HTMLElement>('.el-overlay'))
  if (overlays.some(item => item.getClientRects().length > 0)) {
    return true
  }
  return document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]') !== null
}

export function isEditableElement(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) {
    return false
  }
  return target instanceof HTMLInputElement
    || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement
    || target.isContentEditable
}

/**
 * Focus an input now and re-apply focus a few times while the page settles.
 * `canFocus` is re-evaluated before every attempt, so a dialog that opens in
 * the meantime keeps its focus.
 */
export function focusWithRetry(getInput: () => HTMLInputElement | null | undefined, canFocus: () => boolean = () => true) {
  const apply = () => {
    const input = getInput()
    if (input && canFocus() && !isAnyDialogOpen()) {
      input.focus({ preventScroll: true })
    }
  }
  apply()
  requestAnimationFrame(apply)
  setTimeout(apply, 60)
  setTimeout(apply, 180)
}

export interface ScanCaptureOptions {
  /** The scan input element. */
  input: () => HTMLInputElement | null | undefined
  /** Submit handler, called on Enter when the input has a value. */
  onSubmit: () => void
  /** Keystrokes are ignored while busy (a submission is in flight). */
  busy?: MaybeRefOrGetter<boolean>
  /** Called when Enter is pressed on an empty input, or a key was redirected. */
  refocus: () => void
}

/**
 * Page-level keyboard capture for a scan page: characters typed while focus
 * is on a neutral element are appended to the scan input; Enter submits.
 * Installed while the page is mounted (and active, when kept alive).
 */
export function useScanCapture(options: ScanCaptureOptions) {
  function handleKeydown(event: KeyboardEvent) {
    const input = options.input()
    if (!input || toValue(options.busy) || isAnyDialogOpen()) {
      return
    }
    if (event.ctrlKey || event.altKey || event.metaKey || event.isComposing) {
      return
    }
    const target = event.target
    if (isEditableElement(target) && target !== input) {
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      if (input.value.trim()) {
        options.onSubmit()
      }
      else {
        options.refocus()
      }
      return
    }
    if (event.key.length === 1 && target !== input) {
      event.preventDefault()
      input.value += event.key
      input.dispatchEvent(new Event('input', { bubbles: true }))
      options.refocus()
    }
  }

  // Coming back to the window (alt-tab, unlocking the station) must land the
  // cursor in the scan box again, or the next scan types into nothing.
  function handleWindowFocus() {
    if (!toValue(options.busy)) {
      options.refocus()
    }
  }
  function handleVisibilityChange() {
    if (document.visibilityState === 'visible') {
      handleWindowFocus()
    }
  }

  let installed = false
  function install() {
    if (!installed) {
      document.addEventListener('keydown', handleKeydown, true)
      window.addEventListener('focus', handleWindowFocus)
      document.addEventListener('visibilitychange', handleVisibilityChange)
      installed = true
    }
  }
  function uninstall() {
    if (installed) {
      document.removeEventListener('keydown', handleKeydown, true)
      window.removeEventListener('focus', handleWindowFocus)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      installed = false
    }
  }

  onMounted(install)
  onActivated(install)
  onDeactivated(uninstall)
  onBeforeUnmount(uninstall)

  return { handleKeydown }
}

/** Station identity for field writes, shared with the previous UI (localStorage `pts_station_id_v2`). */
export function getStationId() {
  const key = 'station_id_v2'
  let id = localStorage.getItem(key)
  if (!id) {
    const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`
    id = `station-${random}`
    localStorage.setItem(key, id)
  }
  return id
}
