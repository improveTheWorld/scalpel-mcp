import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

/**
 * Découpe une chaîne en lignes pour une opération de remplacement ou d'insertion.
 */
function splitContentLines(content) {
  if (content === undefined || content === null) return [];
  const s = String(content);
  if (s === '') return [''];
  let raw = s;
  if (raw.endsWith('\r\n')) raw = raw.slice(0, -2);
  else if (raw.endsWith('\n') || raw.endsWith('\r')) raw = raw.slice(0, -1);
  return raw.split(/\r?\n/);
}

/**
 * Applique des opérations de ligne 1-based (replace, delete, insert_after) de bas en haut.
 *
 * @param {string} text - Contenu initial
 * @param {Array<object>} ops - Opérations de ligne
 * @returns {{ text: string | null, error: string | null }}
 */
export function applyLineOps(text, ops) {
  if (!Array.isArray(ops)) {
    return { text: null, error: 'ops must be an array' };
  }
  if (typeof text !== 'string') {
    return { text: null, error: 'text must be a string' };
  }

  const newline = text.includes('\r\n') ? '\r\n' : '\n';
  const trailing = text.endsWith('\n') || text.endsWith('\r');

  let raw = text;
  if (trailing) {
    if (raw.endsWith('\r\n')) raw = raw.slice(0, -2);
    else if (raw.endsWith('\n') || raw.endsWith('\r')) raw = raw.slice(0, -1);
  }
  const lines = raw === '' ? [] : raw.split(/\r?\n/);
  const total = lines.length;

  if (ops.length === 0) {
    return { text, error: null };
  }

  const actions = [];
  const insertPoints = new Set();

  for (let index = 0; index < ops.length; index++) {
    const op = ops[index];
    if (!op || typeof op !== 'object') {
      return { text: null, error: `op ${index}: expected an object.` };
    }

    try {
      if ('replace' in op) {
        if (!Array.isArray(op.replace) || op.replace.length < 2) {
          return { text: null, error: `op ${index}: replace range must be an array [start, end].` };
        }
        const first = Number(op.replace[0]);
        const last = Number(op.replace[1]);
        if (!Number.isInteger(first) || !Number.isInteger(last) || first < 1 || last < first || last > total) {
          return {
            text: null,
            error: `op ${index}: replace range must be inside 1..${total}.`
          };
        }
        actions.push({
          start: first - 1,
          end: last,
          replacement: splitContentLines(op.content),
          isRange: true,
          first,
          last,
          index
        });
      } else if ('delete' in op) {
        if (!Array.isArray(op.delete) || op.delete.length < 2) {
          return { text: null, error: `op ${index}: delete range must be an array [start, end].` };
        }
        const first = Number(op.delete[0]);
        const last = Number(op.delete[1]);
        if (!Number.isInteger(first) || !Number.isInteger(last) || first < 1 || last < first || last > total) {
          return {
            text: null,
            error: `op ${index}: delete range must be inside 1..${total}.`
          };
        }
        actions.push({
          start: first - 1,
          end: last,
          replacement: [],
          isRange: true,
          first,
          last,
          index
        });
      } else if ('insert_after' in op) {
        const position = Number(op.insert_after);
        if (!Number.isInteger(position) || position < 0 || position > total) {
          return {
            text: null,
            error: `op ${index}: insert_after must be inside 0..${total}.`
          };
        }
        if (insertPoints.has(position)) {
          return {
            text: null,
            error: `op ${index}: another insert already targets position ${position}.`
          };
        }
        insertPoints.add(position);
        actions.push({
          start: position,
          end: position,
          replacement: splitContentLines(op.content),
          isRange: false,
          position,
          index
        });
      } else {
        return {
          text: null,
          error: `op ${index}: expected replace, delete or insert_after.`
        };
      }
    } catch {
      return { text: null, error: `op ${index}: malformed range or content.` };
    }
  }

  // Vérification du chevauchement entre plages
  const ranges = actions.filter(a => a.isRange);
  for (let i = 0; i < ranges.length; i++) {
    for (let j = i + 1; j < ranges.length; j++) {
      if (ranges[i].start < ranges[j].end && ranges[j].start < ranges[i].end) {
        return {
          text: null,
          error: `operations overlap: [${ranges[i].first}, ${ranges[i].last}] and [${ranges[j].first}, ${ranges[j].last}].`
        };
      }
    }
  }

  // Tri bottom-up : ordre décroissant d'indice de départ.
  // Si un insert et un range partagent le même indice start, le range s'applique d'abord.
  actions.sort((a, b) => {
    if (b.start !== a.start) return b.start - a.start;
    return (b.isRange ? 1 : 0) - (a.isRange ? 1 : 0);
  });

  for (const action of actions) {
    lines.splice(action.start, action.end - action.start, ...action.replacement);
  }

  const result = lines.join(newline) + (trailing && lines.length > 0 ? newline : '');
  return { text: result, error: null };
}

/**
 * Calcule les opcodes de comparaison entre deux listes de lignes (LCS simple).
 */
function diffLines(oldLines, newLines) {
  const m = oldLines.length;
  const n = newLines.length;
  if (m === 0 && n === 0) return [];
  if (m === 0) return [['insert', 0, 0, 0, n]];
  if (n === 0) return [['delete', 0, m, 0, 0]];

  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let i = 0; i < m; i++) {
    for (let j = 0; j < n; j++) {
      if (oldLines[i] === newLines[j]) {
        dp[i + 1][j + 1] = dp[i][j] + 1;
      } else {
        dp[i + 1][j + 1] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  const matches = [];
  let i = m, j = n;
  while (i > 0 && j > 0) {
    if (oldLines[i - 1] === newLines[j - 1]) {
      matches.push([i - 1, j - 1]);
      i--;
      j--;
    } else if (dp[i - 1][j] >= dp[i][j - 1]) {
      i--;
    } else {
      j--;
    }
  }
  matches.reverse();

  const opcodes = [];
  let curI = 0, curJ = 0;
  for (const [mi, mj] of matches) {
    if (curI < mi && curJ < mj) {
      opcodes.push(['replace', curI, mi, curJ, mj]);
    } else if (curI < mi) {
      opcodes.push(['delete', curI, mi, curJ, curJ]);
    } else if (curJ < mj) {
      opcodes.push(['insert', curI, curI, curJ, mj]);
    }
    opcodes.push(['equal', mi, mi + 1, mj, mj + 1]);
    curI = mi + 1;
    curJ = mj + 1;
  }
  if (curI < m && curJ < n) {
    opcodes.push(['replace', curI, m, curJ, n]);
  } else if (curI < m) {
    opcodes.push(['delete', curI, m, curJ, curJ]);
  } else if (curJ < n) {
    opcodes.push(['insert', curI, curI, curJ, n]);
  }

  return opcodes;
}

/**
 * Génère le snippet compact diff standard Scalpel.
 */
export function renderCompactDiff(targetPath, oldText, newText, newHash, context = 2, maxFullLines = 20) {
  function toLines(s) {
    if (!s) return [];
    let raw = s;
    if (raw.endsWith('\r\n')) raw = raw.slice(0, -2);
    else if (raw.endsWith('\n') || raw.endsWith('\r')) raw = raw.slice(0, -1);
    return raw === '' ? [] : raw.split(/\r?\n/);
  }

  const oldLines = toLines(oldText);
  const newLines = toLines(newText);
  const linesCount = newLines.length;

  let diffSnippet = '';
  if (oldLines.length === 0 && newLines.length === 0) {
    diffSnippet = '(empty block)';
  } else if (oldText === newText) {
    diffSnippet = '(no changes)';
  } else {
    const width = String(Math.max(1, linesCount)).length;
    const spacePad = ' '.repeat(width);

    if (oldLines.length === 0) {
      diffSnippet = newLines.map((l, idx) => `+ ${String(idx + 1).padStart(width, ' ')} | ${l}`).join('\n');
    } else if (newLines.length === 0) {
      diffSnippet = oldLines.map(l => `- ${spacePad} | ${l}`).join('\n');
    } else {
      const opcodes = diffLines(oldLines, newLines);
      const diffOpcodes = opcodes.filter(op => op[0] !== 'equal');

      if (diffOpcodes.length === 0) {
        diffSnippet = '(no changes)';
      } else {
        let hunks = [];
        if (newLines.length <= maxFullLines) {
          hunks = [[0, newLines.length]];
        } else {
          const rawHunks = [];
          for (const [tag, i1, i2, j1, j2] of diffOpcodes) {
            const hStart = Math.max(0, j1 - context);
            const hEnd = Math.min(newLines.length, j2 + context);
            rawHunks.push([hStart, hEnd]);
          }

          const merged = [];
          for (const h of rawHunks) {
            if (merged.length === 0) {
              merged.push(h);
            } else {
              const prev = merged[merged.length - 1];
              if (h[0] <= prev[1] + 1) {
                prev[1] = Math.max(prev[1], h[1]);
              } else {
                merged.push(h);
              }
            }
          }
          hunks = merged;
        }

        const newTags = {};
        for (const [tag, i1, i2, j1, j2] of opcodes) {
          const t = (tag === 'insert' || tag === 'replace') ? '+' : ' ';
          for (let jIdx = j1; jIdx < j2; jIdx++) {
            newTags[jIdx] = t;
          }
        }

        const deletedMap = [];
        for (const [tag, i1, i2, j1, j2] of opcodes) {
          if (tag === 'delete' || tag === 'replace') {
            for (let iIdx = i1; iIdx < i2; iIdx++) {
              deletedMap.push([j1, oldLines[iIdx]]);
            }
          }
        }

        const out = [];
        let lastEnd = 0;

        for (const [hStart, hEnd] of hunks) {
          if (hStart > lastEnd) {
            const omitted = hStart - lastEnd;
            if (lastEnd === 0) {
              out.push(`  ... (${omitted} preceding lines omitted)`);
            } else {
              out.push(`  ... (${omitted} lines omitted)`);
            }
          }

          for (let jIdx = hStart; jIdx < hEnd; jIdx++) {
            for (const [atPos, delLine] of deletedMap) {
              if (atPos === jIdx) {
                out.push(`- ${spacePad} | ${delLine}`);
              }
            }
            const tag = newTags[jIdx] || ' ';
            out.push(`${tag} ${String(jIdx + 1).padStart(width, ' ')} | ${newLines[jIdx]}`);
          }

          if (hEnd === newLines.length) {
            for (const [atPos, delLine] of deletedMap) {
              if (atPos >= newLines.length) {
                out.push(`- ${spacePad} | ${delLine}`);
              }
            }
          }

          lastEnd = hEnd;
        }

        if (lastEnd < newLines.length) {
          const omitted = newLines.length - lastEnd;
          out.push(`  ... (${omitted} following lines omitted)`);
        }

        diffSnippet = out.join('\n');
      }
    }
  }

  const shortHash = (newHash || '').slice(0, 8);
  return `[ok] ${targetPath} [h:${shortHash}] (${linesCount} lines)\n${diffSnippet}`;
}

/**
 * Modifie un fichier sans grammaire sous forme de bloc unique.
 */
export async function editFileBlock({ absPath, ops, expectedHash, fsApi = fs.promises }) {
  let buf;
  let originalMode = null;
  try {
    const st = await fsApi.stat(absPath);
    if (st.isDirectory()) {
      throw new Error(`Cannot edit directory "${absPath}" as a file block.`);
    }
    originalMode = st.mode;
    buf = await fsApi.readFile(absPath);
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      throw new Error(`File not found: "${absPath}".`);
    }
    throw err;
  }

  const currentHash = crypto.createHash('sha1').update(buf).digest('hex');

  if (expectedHash && expectedHash !== currentHash) {
    throw new Error(
      `Stale read: the block changed since you read it (expected ${expectedHash}, current ${currentHash}) [h:${currentHash}]`
    );
  }

  const hasBOM = buf.length >= 3 && buf[0] === 0xEF && buf[1] === 0xBB && buf[2] === 0xBF;
  const rawText = hasBOM ? buf.subarray(3).toString('utf8') : buf.toString('utf8');

  const { text: newText, error } = applyLineOps(rawText, ops);
  if (error) {
    throw new Error(error);
  }

  const finalText = hasBOM ? '\uFEFF' + newText : newText;
  const newHash = crypto.createHash('sha1').update(Buffer.from(finalText, 'utf8')).digest('hex');

  const tmpPath = `${absPath}.${crypto.randomUUID()}.tmp`;
  try {
    await fsApi.writeFile(tmpPath, finalText, 'utf8');
    if (originalMode !== null && typeof fsApi.chmod === 'function') {
      try {
        await fsApi.chmod(tmpPath, originalMode);
      } catch {}
    }
    await fsApi.rename(tmpPath, absPath);
  } catch (err) {
    try {
      await fsApi.unlink(tmpPath);
    } catch {}
    throw err;
  }

  return renderCompactDiff(absPath, rawText, newText, newHash);
}

/**
 * Supprime un fichier sans grammaire sous forme de bloc unique.
 */
export async function deleteFileBlock({ absPath, expectedHeading, fsApi = fs.promises }) {
  let st;
  try {
    st = await fsApi.stat(absPath);
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      throw new Error(`File not found: "${absPath}".`);
    }
    throw err;
  }

  if (st.isDirectory()) {
    throw new Error(`Cannot delete directory "${absPath}" with delete_block.`);
  }

  if (expectedHeading !== undefined && expectedHeading !== null && expectedHeading !== '') {
    const base = path.basename(absPath);
    const normExpected = path.normalize(expectedHeading).toLowerCase();
    const normBase = base.toLowerCase();
    const normAbs = path.normalize(absPath).toLowerCase();
    if (normExpected !== normBase && !normAbs.endsWith(normExpected)) {
      throw new Error(
        `expected_heading guard failed: expected "${expectedHeading}", target is "${base}".`
      );
    }
  }

  await fsApi.unlink(absPath);
  return `[ok] ${absPath} (deleted)`;
}