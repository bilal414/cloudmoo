# CloudMoo website

Static marketing site for [CloudMoo](https://github.com/bilal414/cloudmoo). No build step — publish this directory as-is.

## Deploy on Cloudflare Pages (dashboard)
1. Connect the GitHub repo; framework preset: **None**.
2. Build command: *(none)* — output directory: `website`.

## Deploy from the CLI
`npx wrangler pages deploy website`

Security headers and CSP live in `_headers`; all assets are local (system fonts, zero external requests).
