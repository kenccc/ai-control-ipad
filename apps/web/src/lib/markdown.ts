/**
 * Markdown rendering for agent messages and Forgejo content.
 *
 * Everything rendered here is untrusted: agent output is model-generated, and issue
 * bodies and comments are written by other people. So the pipeline is parse → rewrite
 * URLs → sanitize, and the sanitizer runs last and unconditionally.
 */
import DOMPurify from 'dompurify'
import { marked } from 'marked'

/** `/attachments/<uuid>` on a Forgejo host, absolute or root-relative. */
const ATTACHMENT_RE =
  /^(?:https?:\/\/[^/]+)?\/attachments\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i

/**
 * Forgejo serves attachments only to authenticated callers and the API token lives on
 * the server, so an <img> pointing straight at Forgejo renders as a broken image (it
 * gets redirected to a login page). Point it at our proxy instead, which adds the
 * token server-side.
 */
export function rewriteAttachmentUrl(url: string): string | null {
  const match = ATTACHMENT_RE.exec(url.trim())
  return match ? `/api/forgejo/attachment/${match[1]}` : null
}

marked.setOptions({ gfm: true, breaks: true })

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    const href = node.getAttribute('href') ?? ''
    // Links open away from a Home Screen PWA, so send them to a real browser tab and
    // deny the opened page any handle back to this one.
    if (/^https?:/i.test(href)) {
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer nofollow')
    } else if (!href.startsWith('#')) {
      node.removeAttribute('href')
    }
  }
  if (node.tagName === 'IMG') {
    const src = node.getAttribute('src') ?? ''
    const proxied = rewriteAttachmentUrl(src)
    if (proxied) {
      node.setAttribute('src', proxied)
      node.setAttribute('loading', 'lazy')
      return
    }
    if (src.startsWith('/api/forgejo/attachment/') || src.startsWith('data:image/')) {
      node.setAttribute('loading', 'lazy')
      return
    }
    // Anything else would be blocked by the page's CSP and would render as a broken
    // image. Degrade to a link so the reader can still reach it, rather than showing
    // a hole in the page.
    const link = node.ownerDocument.createElement('a')
    link.setAttribute('href', src)
    link.textContent = node.getAttribute('alt') || src
    node.replaceWith(link)
  }
})

const ALLOWED_TAGS = [
  'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'ul', 'ol', 'li', 'a', 'img',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'input',  // GFM task-list checkboxes; forced disabled below.
  'span', 'sup', 'sub',
]

export function renderMarkdown(source: string): string {
  if (!source) return ''
  const raw = marked.parse(source, { async: false }) as string
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'target', 'rel', 'loading',
                   'colspan', 'rowspan', 'align', 'type', 'checked', 'disabled',
                   'class', 'start'],
    // No inline event handlers, no javascript:/vbscript: URLs, no <style>.
    FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'svg', 'math'],
    FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick'],
    ALLOW_DATA_ATTR: false,
  })
}

/** True when the text has any markdown structure worth rendering as HTML. */
export function looksLikeMarkdown(text: string): boolean {
  return /(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|\||```)|\*\*|`[^`]+`|\[[^\]]*\]\(/.test(text)
}
