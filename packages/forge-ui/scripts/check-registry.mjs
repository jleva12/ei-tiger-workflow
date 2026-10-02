#!/usr/bin/env node
/**
 * Fails when the committed registry is out of date with the source.
 *
 * Apps install straight from the files committed in `registry.json` and
 * `public/r/`, so a source change pushed without `npm run registry:build`
 * would ship stale components. Run after `registry:build`
 * (`npm run registry:check` does both); CI runs it on every push.
 */
import { execFileSync } from "node:child_process"

const paths = ["registry.json", "public/r"]
const status = execFileSync("git", ["status", "--porcelain", "--", ...paths], {
  encoding: "utf8",
}).trim()

if (status) {
  console.error(
    [
      "The registry is out of date with the source:",
      "",
      status,
      "",
      "Run `npm run registry:build` and commit registry.json and public/r.",
    ].join("\n")
  )
  process.exit(1)
}

console.log("Registry is up to date.")
