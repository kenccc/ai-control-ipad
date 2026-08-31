import { useMemo } from 'react'
import { renderMarkdown } from '../lib/markdown'

interface Props {
  children: string
  className?: string
  /** Called with an image URL when the reader taps it, for the lightbox. */
  onImageClick?: (src: string, alt: string) => void
}

export function Markdown({ children, className, onImageClick }: Props) {
  const html = useMemo(() => renderMarkdown(children), [children])

  return (
    <div
      className={`md ${className ?? ''}`}
      dangerouslySetInnerHTML={{ __html: html }}
      onClick={(event) => {
        const target = event.target as HTMLElement
        if (target.tagName === 'IMG' && onImageClick) {
          event.preventDefault()
          onImageClick(target.getAttribute('src') ?? '',
                       target.getAttribute('alt') ?? '')
        }
      }}
    />
  )
}
