import type { PropsWithChildren } from 'react'

type HoverClickableWrapperProps = PropsWithChildren<{
  className?: string
}>

const HoverClickableWrapper = ({
  children,
  className
}: HoverClickableWrapperProps): React.JSX.Element => {
  const handleMouseEnter = (): void => {
    window.api.setWindowClickable(true)
  }

  const handleMouseLeave = (): void => {
    window.api.setWindowClickable(false)
  }

  return (
    <div className={className} onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}>
      {children}
    </div>
  )
}

export default HoverClickableWrapper
