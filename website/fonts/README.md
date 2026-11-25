# Mavrick website fonts

OTF Glusp Bold is the bundled display face. Antonio, Manrope, and IBM Plex Mono are self-hosted Latin WOFF2 fallbacks, with their SIL Open Font License notices in this directory.

## Add Migha and Heming

Place licensed webfont files named `Migha-Bold.woff2` and `Heming-Variable.woff2` in this directory. In [styles.css](../styles.css), update the matching `@font-face` sources.

```css
/* Migha */
src: local("Migha Bold"), url("fonts/Migha-Bold.woff2") format("woff2");

/* Heming */
src: local("Heming Variable"), url("fonts/Heming-Variable.woff2") format("woff2");
```

For static Heming files, add one face per installed weight. Keep the family names `Migha` and `Heming` so the page and the printed glass HUD use the same typefaces. The website waits for its HUD fonts before the first canvas paint.
