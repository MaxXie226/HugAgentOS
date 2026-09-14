import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { t } from '../src/i18n';
import { DesktopUpdateEntry } from '../src/desktop/DesktopUpdateEntry';

const render = (available_version: string | null, busy = false) => renderToStaticMarkup(
  <DesktopUpdateEntry status={{ available_version, busy }} className="test">
    <span>Help menu</span>
  </DesktopUpdateEntry>,
);
assert.match(render(null), /Help menu/);
assert.ok(!render(null).includes(t('下载更新')));
assert.ok(render('1.2.3').includes(t('下载更新')));
assert.match(render('1.2.3'), /1.2.3/);
assert.doesNotMatch(render('1.2.3'), /Help menu/);
assert.match(render('1.2.3', true), /disabled/);
console.log('desktop update entry checks passed');
