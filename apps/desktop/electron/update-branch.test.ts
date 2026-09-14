import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { DEFAULT_UPDATE_BRANCH, normalizeUpdateBranch, resolveUpdateBranch, updateBranchArgs } from './update-branch'

test('configuration and updater arguments preserve explicit branches and share the default', async () => {
  for (const value of [undefined, null, '', '  ', 42, DEFAULT_UPDATE_BRANCH]) {
    const branch = await resolveUpdateBranch(
      value,
      async () => assert.fail('the release channel must not fall back to a different branch'),
      () => assert.fail('default selection needs no config write')
    )
    assert.equal(branch, DEFAULT_UPDATE_BRANCH)
    assert.deepEqual(updateBranchArgs(normalizeUpdateBranch(value)), updateBranchArgs(branch))
  }

  for (const value of ['main', 'release/custom']) {
    const probes: string[] = []
    const branch = await resolveUpdateBranch(
      ` ${value} `,
      async target => {
        probes.push(target)
        return 0
      },
      () => assert.fail('an existing explicit branch must remain selected')
    )
    assert.deepEqual(probes, [value])
    assert.deepEqual(updateBranchArgs(branch), ['--branch', value])
  }
})

test('only a definitively deleted custom ref returns to the release channel', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-branch-'))
  const persisted: string[] = []
  const git = (...args: string[]) => spawnSync('git', args, { cwd: root, encoding: 'utf8' })

  try {
    assert.equal(git('init', '-b', DEFAULT_UPDATE_BRANCH).status, 0)
    assert.equal(git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'base').status, 0)
    assert.equal(git('branch', 'existing').status, 0)
    const probe = async (branch: string) => git('ls-remote', '--exit-code', '--heads', root, branch).status!
    const persist = (branch: string) => void persisted.push(branch)

    const deleted = await resolveUpdateBranch('deleted', probe, persist)
    assert.deepEqual(updateBranchArgs(deleted), ['--branch', DEFAULT_UPDATE_BRANCH])
    assert.deepEqual(persisted, [DEFAULT_UPDATE_BRANCH])
    assert.equal(await resolveUpdateBranch('existing', probe, persist), 'existing')

    const unavailable = async (branch: string) => git('ls-remote', '--exit-code', '--heads', path.join(root, 'missing-remote'), branch).status!
    assert.equal(await resolveUpdateBranch('existing', unavailable, persist), 'existing')
    assert.deepEqual(persisted, [DEFAULT_UPDATE_BRANCH])
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
