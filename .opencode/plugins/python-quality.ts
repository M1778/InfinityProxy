// Plankton-style write-time quality enforcement for InfinityProxy (Python).
// - After any edit/write of a .py file: auto-format with ruff (silent), then
//   collect leftover ruff violations and surface them.
// - Before any edit/write: block tampering with linter configs and with the
//   [tool.ruff] section of pyproject.toml, so rules cannot be silently
//   loosened to make violations pass.
// Guarded so a missing ruff or a resolved path weirdness never breaks a session.
import type { Plugin } from "@opencode-ai/plugin"

const PROTECTED_CONFIGS = [
  ".ruff.toml",
  "biome.json",
  ".shellcheckrc",
  ".yamllint",
  ".hadolint.yaml",
  ".markdownlint.json",
  ".markdownlint.yaml",
  ".pre-commit-config.yaml",
]

function isPy(path: string | undefined): boolean {
  return typeof path === "string" && path.endsWith(".py")
}

function isProtectedConfig(path: string | undefined): boolean {
  if (typeof path !== "string") return false
  const base = path.split("/").pop() ?? path
  return PROTECTED_CONFIGS.includes(base)
}

function ruffSectionMismatch(current: string, candidate: string): boolean {
  // Compare only the [tool.ruff] section so legitimate dep/version edits to
  // pyproject.toml are never blocked, only rule suppression attempts.
  const section = (text: string): string => {
    const lines = text.split("\n")
    let inside = false
    const out: string[] = []
    for (const line of lines) {
      if (line.trimStart().startsWith("[")) inside = line.includes("[tool.ruff]")
      if (inside) out.push(line)
    }
    return out.join("\n")
  }
  return section(current) !== section(candidate)
}

export const PythonQualityPlugin: Plugin = async ({ $, client }) => {
  const readHeadFile = async (path: string): Promise<string> => {
    try {
      const res = await $`git show HEAD:${path}`.quiet()
      return res.stdout.toString()
    } catch {
      return ""
    }
  }

  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "edit" && input.tool !== "write") return
      const filePath = output.args?.filePath as string | undefined
      const base = filePath?.split("/").pop() ?? ""

      if (isProtectedConfig(base)) {
        throw new Error(
          `[python-quality] blocked: ${filePath} is a protected linter config; ` +
            `loosening rules to pass checks is not allowed. Ask the maintainer ` +
            `to adjust the configured rules instead.`,
        )
      }

      if (base === "pyproject.toml") {
        const head = await readHeadFile("pyproject.toml")
        if (!head) return
        const candidate =
          input.tool === "edit"
            ? head.replace(output.args.oldString ?? "", output.args.newString ?? "")
            : (output.args.content as string)
        if (ruffSectionMismatch(head, candidate)) {
          throw new Error(
            `[python-quality] blocked: the edit changes the [tool.ruff] section ` +
              `of pyproject.toml. Suppressing rules to make checks pass is not ` +
              `allowed; ask the maintainer to adjust the configured rules.`,
          )
        }
      }
    },

    "tool.execute.after": async (input, output) => {
      if (input.tool !== "edit" && input.tool !== "write") return
      const filePath = output.args?.filePath as string | undefined
      if (!isPy(filePath)) return

      try {
        await $`${".venv/bin/ruff"} format ${filePath}`.quiet()
        const check = await $`${".venv/bin/ruff"} check ${filePath} --output-format=json`.quiet()
        const leftover = JSON.parse(check.stdout.toString()) as {
          code?: string
          message?: string
        }[]
        if (leftover.length > 0) {
          const count = leftover.length
          const first = leftover[0]
          const detail = first
            ? ` e.g. ${first.code ?? "?"} ${first.message ?? ""}`
            : ""
          await client.app.log({
            body: {
              service: "python-quality",
              level: "warn",
              message: `${count} ruff violation(s) remain in ${filePath} after format.${detail}`,
            },
          })
          try {
            // Best-effort surfacing to the result; ignored if the runtime
            // does not carry a `note` field on tool results.
            ;(output as Record<string, unknown>).note =
              `[python-quality] ${count} ruff violation(s) remain in ` +
              `${filePath} after format (first: ${first?.code ?? "?"} ` +
              `${first?.message ?? ""}).`
          } catch {
            // fall through: log line above is the durable record
          }
        }
      } catch {
        // ruff missing or unrelated harness noise — never break the session
      }
    },
  }
}