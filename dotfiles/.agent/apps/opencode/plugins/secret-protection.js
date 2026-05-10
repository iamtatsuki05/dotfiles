const SECRET_PATH_PATTERN = /(^|\/)(\.env(\..*)?|secrets\.env|credentials\.json|secrets\.json|id_rsa|id_ed25519)$|(\.key|\.pem)$/

const hasSecretPath = (value) => {
  if (typeof value === "string") return SECRET_PATH_PATTERN.test(value)
  if (Array.isArray(value)) return value.some(hasSecretPath)
  if (value && typeof value === "object") return Object.values(value).some(hasSecretPath)
  return false
}

export const SecretProtection = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      if ((input.tool === "read" || input.tool === "edit") && hasSecretPath(output.args)) {
        throw new Error("Refusing to access secret-like files")
      }
    },
  }
}
