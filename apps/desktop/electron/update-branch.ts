// Operations release channel; matches hermes_cli.update_target.DEFAULT_UPDATE_BRANCH.
export const DEFAULT_UPDATE_BRANCH = 'current-ops'

export function normalizeUpdateBranch(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value.trim() : DEFAULT_UPDATE_BRANCH
}

export function updateBranchArgs(value: unknown): string[] {
  return ['--branch', normalizeUpdateBranch(value)]
}

export async function resolveUpdateBranch(
  value: unknown,
  probe: (branch: string) => Promise<number>,
  persist: (branch: string) => void
): Promise<string> {
  const branch = normalizeUpdateBranch(value)

  // A missing release channel must fail at fetch, never silently select another channel.
  if (branch === DEFAULT_UPDATE_BRANCH || (await probe(branch)) !== 2) {
    return branch
  }

  // ls-remote exit 2 proves the custom ref is absent; network errors retain the selection.
  persist(DEFAULT_UPDATE_BRANCH)
  return DEFAULT_UPDATE_BRANCH
}
