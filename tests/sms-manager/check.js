'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('packages/luci-app-sms-manager/htdocs/luci-static/resources/view/modem/sms_manager_readsms.js', 'utf8');
String.prototype.format = function(...args) { let i = 0; return this.replace(/%[sd]/g, () => args[i++]); };
String.format = (s, ...args) => s.format(...args);
async function run(mode) {
 const nodes = {}, calls = [], notices = [];
 function E(tag, attrs, children) {
  const n = { tag, attrs: attrs || {}, children: children || [], innerHTML: '', textContent: '', appendChild(c) { this.children.push(c); } };
  if (n.attrs.id) nodes[n.attrs.id] = n;
  return n;
 }
 const values = { readport: '/org/freedesktop/ModemManager1/Modem/0' };
 const uci = { load: async () => [], get: (c,s,k) => values[k], set: (c,s,k,v) => values[k] = v, save: async()=>{}, apply:async()=>{} };
 async function exec(command, args = []) {
  calls.push(args);
  if (args.includes('-L')) return { code: 0, stdout: '/org/freedesktop/ModemManager1/Modem/17 [test] modem\n' };
  if (args.includes('--messaging-list-sms')) {
   if (args[1] !== '17') return { code:1, stderr: "couldn't find modem" };
   if (mode === 'error') return { code:1, stderr: 'backend unavailable' };
   return { code:0, stdout: mode === 'empty' ? 'No sms messages were found' : '/org/freedesktop/ModemManager1/SMS/42 (received)' };
  }
  return { code:0, stdout:'number: 10010\ntext: Test SMS\n------------------\nProperties | storage: sm\ntimestamp: 2026-11-26T12:00:00+08\n' };
 }
 const proto = new Function('view','fs','uci','ui','rpc','L','E','_','document', source)(
  {extend:x=>x}, {exec, exec_direct:async(c,a)=>{ const r=await exec(c,a); if(r.code)throw Error(r.stderr); return r.stdout;}}, uci,
  {createHandlerFn:()=>()=>{},addNotification:(...a)=>notices.push(a)}, {declare:()=>()=>{}},
  {resolveDefault:(p,d)=>p.catch(()=>d),resource:x=>x}, E, x=>x, {getElementById:id=>nodes[id]});
 await proto.render();
 await new Promise(resolve=>setImmediate(resolve));
 return { nodes, calls, notices };
}
(async()=>{
 let r=await run('messages');
 assert(r.calls.some(a=>a[1]==='17' && a.includes('--messaging-list-sms')), 'must resolve stale modem before listing');
 assert(r.nodes.smsTable.children.length > 1, 'must render message row');
 assert.match(r.nodes['storage-info'].innerHTML || r.nodes['storage-info'].textContent, /SIM: 1/);
 r=await run('empty');
 assert.match(r.nodes['storage-info'].innerHTML || r.nodes['storage-info'].textContent, /SIM: 0/);
 r=await run('error');
 assert(r.notices.length, 'must report list failure');
 assert.match(r.nodes['storage-info'].textContent, /Error/);
 console.log('SMS reader: stale modem, messages, empty list and backend error passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
