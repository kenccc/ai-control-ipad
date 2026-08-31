import { describe, expect, it } from 'vitest'
import { renderMarkdown, rewriteAttachmentUrl } from '../lib/markdown'

describe('forgejo attachments', () => {
  it('rewrites attachment URLs onto the authenticated proxy', () => {
    const id = '0aea0000-7b27-46fe-b692-958aa9bbec00'
    expect(rewriteAttachmentUrl(`/attachments/${id}`))
      .toBe(`/api/forgejo/attachment/${id}`)
    expect(rewriteAttachmentUrl(`https://projekt.assetgov.cz/attachments/${id}`))
      .toBe(`/api/forgejo/attachment/${id}`)
  })

  it('leaves anything that is not an attachment alone', () => {
    expect(rewriteAttachmentUrl('/attachments/not-a-uuid')).toBeNull()
    expect(rewriteAttachmentUrl('https://example.com/cat.png')).toBeNull()
    expect(rewriteAttachmentUrl('/attachments/../../etc/passwd')).toBeNull()
  })

  it('renders the exact markdown Forgejo writes for a pasted image', () => {
    const html = renderMarkdown(
      '![image](/attachments/0aea0000-7b27-46fe-b692-958aa9bbec00)')
    expect(html).toContain(
      'src="/api/forgejo/attachment/0aea0000-7b27-46fe-b692-958aa9bbec00"')
    expect(html).toContain('loading="lazy"')
  })

  it('degrades a blocked external image to a link instead of a broken image', () => {
    const html = renderMarkdown('![a badge](https://img.shields.io/badge.svg)')
    // The page CSP forbids remote images, so an <img> would render as a hole.
    expect(html).not.toContain('<img')
    expect(html).toContain('https://img.shields.io/badge.svg')
  })
})

describe('markdown structure', () => {
  it('renders headings, lists, tables and code blocks', () => {
    const html = renderMarkdown([
      '## Findings',
      '',
      '- first',
      '- second',
      '',
      '| # | Status |',
      '|---|--------|',
      '| 1 | Fixed  |',
      '',
      '```python',
      'def handler():',
      '    return 1',
      '```',
      '',
      'Inline `code` and **bold** and [a link](https://example.com).',
    ].join('\n'))

    expect(html).toContain('<h2')
    expect(html).toContain('<ul>')
    expect(html).toContain('<table>')
    expect(html).toContain('<th')
    expect(html).toContain('<pre>')
    expect(html).toContain('<code')
    expect(html).toContain('<strong>')
  })

  it('opens external links in a new tab with no handle back to this page', () => {
    const html = renderMarkdown('[docs](https://example.com)')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer nofollow"')
  })
})

describe('sanitization', () => {
  it('strips scripts, event handlers and javascript: URLs', () => {
    const html = renderMarkdown([
      '<script>alert(1)</script>',
      '<img src=x onerror="alert(1)">',
      '[click](javascript:alert(1))',
      '<iframe src="https://evil.example"></iframe>',
      '<div style="position:fixed">styled</div>',
    ].join('\n\n'))

    expect(html).not.toContain('<script')
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('<iframe')
    expect(html).not.toContain('style=')
  })

  it('does not let an SVG payload through', () => {
    const html = renderMarkdown('<svg><script>alert(1)</script></svg>')
    expect(html).not.toContain('<svg')
    expect(html).not.toContain('<script')
  })
})
