/**
 * Where apps install Forge UI from: the registry files committed to the
 * private jleva12/forge-ui repo, read through raw.githubusercontent.com with
 * a GitHub token. Shown in the install dialog and on the overview page.
 */
export const registryRepo = "jleva12/forge-ui"

export const registryUrl = `https://raw.githubusercontent.com/${registryRepo}/main/public/r/{name}.json`

export const registryConfig = `{
  "registries": {
    "@forge-ui": {
      "url": "${registryUrl}",
      "headers": { "Authorization": "Bearer \${FORGE_UI_TOKEN}" }
    }
  }
}`

export const registryEnv = `# .env.local — a GitHub token that can read ${registryRepo}
FORGE_UI_TOKEN=github_pat_…`
