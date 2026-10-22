import path from 'node:path';

export interface IPatterns { include: string[]; exclude: string[] }

function characterExpression(character: string, paths: boolean): string {
  if (character === '*') return paths ? '[^/]*' : '.*';
  if (character === '?') return paths ? '[^/]' : '.';
  return /[\\^$.*+?()[\]{}|]/.test(character) ? '\\' + character : character;
}

function expression(pattern: string, paths: boolean): RegExp {
  let result = '';
  for (let index = 0; index < pattern.length;) {
    if (paths && pattern.startsWith('**/', index)) { result += '(?:.*/)?'; index += 3; continue; }
    if (paths && pattern.startsWith('**', index)) { result += '.*'; index += 2; continue; }
    const character = String.fromCodePoint(pattern.codePointAt(index)!);
    result += characterExpression(character, paths);
    index += character.length;
  }
  return new RegExp('^' + result + '$', 'su');
}

/** Apply the common Unicode glob semantics; exclusions also override annotations. */
export class Selection {
  readonly #include: RegExp[];
  readonly #exclude: RegExp[];

  constructor(patterns: IPatterns, paths = false) {
    this.#include = patterns.include.map(value => expression(value, paths));
    this.#exclude = patterns.exclude.map(value => expression(value, paths));
  }

  accepts(value: string, annotated = false): boolean {
    const matches = (patterns: RegExp[]) => patterns.some(pattern => pattern.exec(value)?.[0].length === value.length);
    return (annotated || matches(this.#include)) && !matches(this.#exclude);
  }
}

/** Resolve original project names without treating producer or dependency code as application source. */
export function sourceName(filename: string, root: string): string | undefined {
  const relative = path.relative(root, filename).split(path.sep).join('/');
  if (!relative || relative.startsWith('../') || path.isAbsolute(relative)
      || relative.split('/').some(value => value === 'node_modules' || value === '.venv')) return;
  return relative;
}
