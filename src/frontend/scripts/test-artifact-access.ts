import assert from 'node:assert/strict';
import { registerLocalChat, registerLocalProject, setHybridDual } from '../src/api';
import { useDeploymentModeStore } from '../src/stores/deploymentModeStore';
import { artifactUrl, artifactOrigin, openLocalArtifact } from '../src/utils/artifactAccess';

useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'dual', activeLocal: false });
setHybridDual(true);
registerLocalChat('local-chat');
registerLocalProject('local-project');
const file = { file_id: 'report', url: '/files/report?revision=2#page=3', chat_id: 'local-chat' };
assert.equal(artifactOrigin(file), 'local');
assert.equal(artifactUrl(file), '/api/files/report?revision=2&hg_target=local#page=3');
assert.equal(artifactUrl(file, { preview: true, params: { format: 'pdf' } }),
  '/api/files/report/preview?revision=2&hg_target=local&format=pdf#page=3');
assert.equal(artifactOrigin({ ...file, origin: 'cloud' }), 'cloud');
assert.equal(artifactUrl({ ...file, origin: 'cloud' }), '/api/files/report?revision=2#page=3');
assert.equal(artifactOrigin({ ...file, url: 'https://cloud.example/files/report' }), 'cloud');
assert.equal(artifactUrl({ ...file, url: 'https://cloud.example/files/report' }), 'https://cloud.example/files/report');
assert.equal(artifactOrigin({ url: '/v1/projects/local-project/local-files/raw?path=report.pdf' }), 'local');
const requests: string[] = [];
globalThis.fetch = async (url) => {
  requests.push(String(url));
  return Response.json({ path: '/Users/test/Reports/年度 报告.pdf', folder_path: '/Users/test/Reports' });
};
const navigations: string[] = [];
await openLocalArtifact(file, false, (url) => navigations.push(url));
await openLocalArtifact(file, true, (url) => navigations.push(url));
assert.deepEqual(requests, ['/api/files/report/local-path?hg_target=local', '/api/files/report/local-path?hg_target=local']);
assert.equal(navigations[0], '/__desktop/open-path?path=%2FUsers%2Ftest%2FReports%2F%E5%B9%B4%E5%BA%A6%20%E6%8A%A5%E5%91%8A.pdf');
assert.equal(navigations[1], '/__desktop/open-path?path=%2FUsers%2Ftest%2FReports');
await assert.rejects(openLocalArtifact({ ...file, origin: 'cloud' }, false, () => assert.fail('must not navigate')));
globalThis.fetch = async () => new Response(JSON.stringify({ detail: '文件已移动或删除' }), { status: 404 });
await assert.rejects(openLocalArtifact(file, false, () => assert.fail('must not navigate')), /文件已移动或删除/);
useDeploymentModeStore.setState({ isDesktop: false, provisionMode: '', activeLocal: false });
assert.equal(artifactOrigin(file), 'cloud');
assert.equal(artifactUrl(file), '/api/files/report?revision=2#page=3');
useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'local_only', activeLocal: true });
setHybridDual(false);
assert.equal(artifactOrigin({ file_id: 'local-only' }), 'local');
assert.equal(artifactUrl({ file_id: 'local-only' }), '/api/files/local-only');
console.log('artifact access: local/cloud routing, query/fragment, native open/folder, missing files passed');

// Reject tool-provided executable URLs or direct native-action sentinels.
for (const url of ['https://[', '//[broken', 'javascript:alert(1)', 'file:///tmp/report', 'data:text/html,bad',
  '/__desktop/open-path?path=/private', 'http://127.0.0.1:1234/__desktop/open-path?path=/private']) {
  assert.equal(artifactUrl({ url, origin: 'cloud' }), '');
}
const { extractArtifactOutputs } = await import('../src/utils/fileParser');
assert.equal(extractArtifactOutputs({ metadata: { origin: 'cloud' }, files: [{ file_id: 'cloud-report', url: '/files/cloud-report' }] })[0].origin, 'cloud');
const { useCanvasStore } = await import('../src/stores/canvasStore');
useDeploymentModeStore.setState({ isDesktop: true, provisionMode: 'dual', activeLocal: false });
setHybridDual(true);
useCanvasStore.getState().openCanvas({ file_id: 'same-id', name: 'Local.pdf', url: '/files/same-id', chat_id: 'local-chat' });
const localTab = useCanvasStore.getState().activeTabId!;
useCanvasStore.getState().openCanvas({ file_id: 'same-id', name: 'Cloud.pdf', url: '/files/same-id', origin: 'cloud' });
assert.equal(useCanvasStore.getState().tabs.length, 2);
useCanvasStore.getState().activateTab(localTab);
assert.equal(useCanvasStore.getState().artifact?.origin, 'local');
globalThis.fetch = async (url) => {
  assert.equal(String(url), '/api/v1/projects/local-project/local-files/location?path=report.pdf&hg_target=local');
  return Response.json({ code: 200, data: { path: '/project/report.pdf', folder_path: '/project' } });
};
await openLocalArtifact({ url: '/v1/projects/local-project/local-files/raw?path=report.pdf' }, true,
  (url) => assert.equal(url, '/__desktop/open-path?path=%2Fproject'));
console.log('artifact source and canvas ownership survive navigation; project API envelope supported');
