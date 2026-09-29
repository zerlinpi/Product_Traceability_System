/**
 * User feedback helpers: toasts and confirmations.
 *
 * Every page reports success and failure the same way the previous UI did:
 * a short title plus an optional detail line, errors carrying the server's
 * own message (the backend already words them for the operator).
 */
import { ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api'

export function notifySuccess(title: string, description = '') {
  useFaToast().success(title, description ? { description } : undefined)
}

export function notifyInfo(title: string, description = '') {
  useFaToast().info(title, description ? { description } : undefined)
}

export function notifyWarning(title: string, description = '') {
  useFaToast().warning(title, description ? { description } : undefined)
}

export function notifyError(error: unknown, title = '操作失败') {
  useFaToast().error(title, { description: errorMessage(error) })
}

/**
 * Ask before a destructive or irreversible action. Resolves to `true` when the
 * operator confirmed, `false` when they cancelled or closed the dialog.
 */
export async function confirmAction(message: string, options: { title?: string, confirmText?: string, danger?: boolean } = {}) {
  try {
    await ElMessageBox.confirm(message, options.title ?? '请确认', {
      confirmButtonText: options.confirmText ?? '确定',
      cancelButtonText: '取消',
      type: options.danger ? 'warning' : 'info',
      confirmButtonClass: options.danger ? 'el-button--danger' : undefined,
      autofocus: false,
      closeOnClickModal: false,
    })
    return true
  }
  catch {
    return false
  }
}

/**
 * Prompt for a required reason (暂扣原因、删除原因…). Resolves to the trimmed
 * text, or `null` when cancelled.
 */
export async function promptReason(message: string, options: { title?: string, placeholder?: string, maxLength?: number } = {}) {
  try {
    const { value } = await ElMessageBox.prompt(message, options.title ?? '请填写原因', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputType: 'textarea',
      inputPlaceholder: options.placeholder ?? '必填',
      inputValidator: (text: string) => {
        const trimmed = String(text ?? '').trim()
        if (!trimmed) {
          return '请填写原因'
        }
        if (trimmed.length > (options.maxLength ?? 200)) {
          return `不能超过 ${options.maxLength ?? 200} 个字符`
        }
        return true
      },
      closeOnClickModal: false,
    })
    return String(value ?? '').trim()
  }
  catch {
    return null
  }
}
