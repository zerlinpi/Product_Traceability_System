/**
 * Print one element (a QR label sheet, a code set) with the browser's print
 * dialog, hiding the rest of the application.
 *
 * Implemented with print CSS (see `.pts-printing` in assets/styles/pts.css)
 * instead of writing HTML into a new window: the page's CSP forbids inline
 * scripts and styles, and a same-document print keeps the authenticated
 * same-origin QR image URLs working without re-downloading them.
 */
export function printElement(element: HTMLElement | null | undefined) {
  if (!element) {
    return
  }
  element.classList.add('pts-print-root')
  document.body.classList.add('pts-printing')
  const cleanup = () => {
    element.classList.remove('pts-print-root')
    document.body.classList.remove('pts-printing')
    window.removeEventListener('afterprint', cleanup)
  }
  window.addEventListener('afterprint', cleanup)
  // Let the class changes apply before the print snapshot is taken.
  requestAnimationFrame(() => {
    window.print()
    // Browsers that do not fire `afterprint` still get cleaned up.
    setTimeout(cleanup, 1000)
  })
}
