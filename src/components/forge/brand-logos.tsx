import * as React from "react"

/*
 * Brand marks for third-party integrations, drawn inline so they stay crisp
 * at any size and need no image hosting. Multi-colour marks keep their
 * official colours; single-colour marks that are black in the original
 * (GitHub) use `currentColor` so they flip with light and dark.
 *
 * The marks are trademarks of their owners: use them only to refer to the
 * product (for example "Connect GitHub"), never as your own branding.
 */

type LogoProps = Omit<React.ComponentProps<"svg">, "viewBox"> & {
  /** Pixel size of the square box the mark is fitted into. Default 20. */
  size?: number
}

function Logo({
  size = 20,
  viewBox,
  children,
  ...props
}: LogoProps & { viewBox: string }) {
  return (
    <svg
      data-slot="brand-logo"
      viewBox={viewBox}
      width={size}
      height={size}
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  )
}

function GitHubLogo(props: LogoProps) {
  return (
    <Logo viewBox="0 0 24 24" {...props}>
      <path
        fill="currentColor"
        d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"
      />
    </Logo>
  )
}

function GitLabLogo(props: LogoProps) {
  return (
    <Logo viewBox="0 0 24 24" {...props}>
      <path
        fill="#FC6D26"
        d="m23.6004 9.5927-.0337-.0862L20.3.9814a.851.851 0 0 0-.3362-.405.8748.8748 0 0 0-.9997.0539.8748.8748 0 0 0-.29.4399l-2.2055 6.748H7.5375l-2.2057-6.748a.8573.8573 0 0 0-.29-.4412.8748.8748 0 0 0-.9997-.0537.8585.8585 0 0 0-.3362.4049L.4332 9.5015l-.0325.0862a6.0657 6.0657 0 0 0 2.0119 7.0105l.0113.0087.03.0213 4.976 3.7264 2.462 1.8633 1.4995 1.1321a1.0085 1.0085 0 0 0 1.2197 0l1.4995-1.1321 2.4619-1.8633 5.006-3.7489.0125-.01a6.0682 6.0682 0 0 0 2.0094-7.003z"
      />
    </Logo>
  )
}

function JiraLogo(props: LogoProps) {
  // A unique gradient id so several logos can share a page. The gradient is
  // relative to each shape's own box, so both lower chevrons reuse it.
  const gradient = React.useId()
  return (
    <Logo viewBox="0 0 24 24" {...props}>
      <defs>
        <linearGradient id={gradient} x1=".9" y1=".05" x2=".35" y2=".6">
          <stop offset=".18" stopColor="#0052CC" />
          <stop offset="1" stopColor="#2684FF" />
        </linearGradient>
      </defs>
      <path
        fill="#2684FF"
        d="M23.013 0H11.455a5.215 5.215 0 0 0 5.215 5.215h2.129v2.057A5.215 5.215 0 0 0 24 12.483V1.005A1.001 1.001 0 0 0 23.013 0Z"
      />
      <path
        fill={`url(#${gradient})`}
        d="M17.294 5.757H5.736a5.215 5.215 0 0 0 5.215 5.214h2.129v2.058a5.218 5.218 0 0 0 5.215 5.214V6.758a1.001 1.001 0 0 0-1.001-1.001Z"
      />
      <path
        fill={`url(#${gradient})`}
        d="M11.571 11.513H0a5.218 5.218 0 0 0 5.232 5.215h2.13v2.057A5.215 5.215 0 0 0 12.575 24V12.518a1.005 1.005 0 0 0-1.005-1.005Z"
      />
    </Logo>
  )
}

function LinearLogo(props: LogoProps) {
  return (
    <Logo viewBox="0 0 24 24" {...props}>
      <path
        fill="#5E6AD2"
        d="M.403 13.795A11.99 11.99 0 0 0 10.205 23.597L.403 13.795Zm-.39-2.726 12.918 12.918a11.97 11.97 0 0 0 2.49-.51L.522 8.58a11.97 11.97 0 0 0-.51 2.489Zm1.058-4.187 16.048 16.048c.6-.28 1.17-.61 1.704-.985L2.055 5.177a12.07 12.07 0 0 0-.985 1.705Zm2.078-3.049A12 12 0 1 1 20.098 20.78L3.15 3.833Z"
      />
    </Logo>
  )
}

function FigmaLogo(props: LogoProps) {
  return (
    <Logo viewBox="-9.5 0 57 57" {...props}>
      <path
        fill="#1ABCFE"
        d="M19 28.5a9.5 9.5 0 1 1 19 0 9.5 9.5 0 0 1-19 0Z"
      />
      <path
        fill="#0ACF83"
        d="M0 47.5A9.5 9.5 0 0 1 9.5 38H19v9.5a9.5 9.5 0 1 1-19 0Z"
      />
      <path fill="#FF7262" d="M19 0v19h9.5a9.5 9.5 0 1 0 0-19H19Z" />
      <path
        fill="#F24E1E"
        d="M0 9.5A9.5 9.5 0 0 0 9.5 19H19V0H9.5A9.5 9.5 0 0 0 0 9.5Z"
      />
      <path
        fill="#A259FF"
        d="M0 28.5A9.5 9.5 0 0 0 9.5 38H19V19H9.5A9.5 9.5 0 0 0 0 28.5Z"
      />
    </Logo>
  )
}

function SlackLogo(props: LogoProps) {
  return (
    <Logo viewBox="0 0 24 24" {...props}>
      <path
        fill="#E01E5A"
        d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52Zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313Z"
      />
      <path
        fill="#36C5F0"
        d="M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834Zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312Z"
      />
      <path
        fill="#2EB67D"
        d="M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834Zm-1.268 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312Z"
      />
      <path
        fill="#ECB22E"
        d="M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52Zm0-1.268a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313Z"
      />
    </Logo>
  )
}

function GoogleDriveLogo(props: LogoProps) {
  return (
    <Logo viewBox="0 -4.65 87.3 87.3" {...props}>
      <path
        fill="#0066DA"
        d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8H0c0 1.55.4 3.1 1.2 4.5Z"
      />
      <path
        fill="#00AC47"
        d="M43.65 25 29.9 1.2c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44A9.06 9.06 0 0 0 0 53h27.5Z"
      />
      <path
        fill="#EA4335"
        d="M73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5H59.798l5.852 11.5Z"
      />
      <path
        fill="#00832D"
        d="M43.65 25 57.4 1.2C56.05.4 54.5 0 52.9 0H34.4c-1.6 0-3.15.45-4.5 1.2Z"
      />
      <path
        fill="#2684FC"
        d="M59.8 53H27.5L13.75 76.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2Z"
      />
      <path
        fill="#FFBA00"
        d="m73.4 26.5-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3L43.65 25 59.8 53h27.45c0-1.55-.4-3.1-1.2-4.5Z"
      />
    </Logo>
  )
}

export {
  FigmaLogo,
  GitHubLogo,
  GitLabLogo,
  GoogleDriveLogo,
  JiraLogo,
  LinearLogo,
  SlackLogo,
  type LogoProps,
}
