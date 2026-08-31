import { useEffect } from 'react'

/** Full-screen image view. Attachments are small on a card and unreadable until
 *  opened, which on a touch screen needs a real viewer rather than a new tab. */
export function Lightbox({ src, alt, onClose }:
                         { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="bar" onClick={(e) => e.stopPropagation()}>
        <span className="truncate grow">{alt || 'Attachment'}</span>
        <a className="btn small ghost" href={src} target="_blank" rel="noopener noreferrer">
          Open
        </a>
        <button className="btn small" onClick={onClose}>Close</button>
      </div>
      <div className="stage">
        <img src={src} alt={alt} onClick={(e) => e.stopPropagation()} />
      </div>
    </div>
  )
}
