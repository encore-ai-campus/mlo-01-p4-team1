import './navigation.css';

/** A real page link: replay explicitly, then remain on the final brand screen. */
export function CinematicNavLink(){
  return <a className="p-cinematic-link" href="/platform/intro">
    <svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true" fill="none">
      <circle cx="9" cy="9" r="7.25" stroke="currentColor" strokeWidth="1.1"/>
      <path d="M7 5.8 12 9l-5 3.2V5.8Z" fill="currentColor"/>
    </svg>
    <span>시네마틱 페이지로 이동</span>
  </a>;
}
