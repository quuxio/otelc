// The shared resolver supplies byte-oriented glob regexes, including exclusions.
import path from 'node:path';

export class Selection {
  constructor(matchers) {
    this.include = matchers.include.map(value => new RegExp(value.replace(/^\(\?-u\)/, ''), 's'));
    this.exclude = matchers.exclude.map(value => new RegExp(value.replace(/^\(\?-u\)/, ''), 's'));
  }
  matches(regex, bytes) {
    const match = regex.exec(bytes);
    return match !== null && match[0].length === bytes.length;
  }
  accepts(value, annotated = false) {
    const bytes = Buffer.from(value).toString('latin1');
    return (annotated || this.include.some(regex => this.matches(regex, bytes))) &&
      !this.exclude.some(regex => this.matches(regex, bytes));
  }
}
export function sourceName(filename, root) {
  const relative = path.relative(root, filename).split(path.sep).join('/');
  if (relative.startsWith('../') || path.isAbsolute(relative) || relative.split('/').some(value => ['node_modules', '.venv'].includes(value))) return null;
  return relative;
}
