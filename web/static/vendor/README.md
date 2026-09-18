# Browser dependencies

Files are vendored to keep the UI independent of third-party CDN availability.

| Package | Version | Local file | License |
| --- | --- | --- | --- |
| lucide | 1.47.0 | lucide.min.js | LICENSE-lucide (ISC) |
| marked | 18.0.13 | marked.umd.js | LICENSE-marked (MIT) |
| dompurify | 3.4.15 | purify.min.js | LICENSE-DOMPurify (Apache-2.0 or MPL-2.0) |

Source: the corresponding published npm package, downloaded via cdn.jsdelivr.net.
DOMPurify sanitizes all rendered Markdown. Do not render raw model output as HTML.
