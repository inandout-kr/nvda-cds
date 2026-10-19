/* Offline regression checks for fail-closed display and sparse observations.
   Run: node scripts/test_frontend.cjs [optional-v2-json-path] */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname,"../docs/index.html"),"utf8");
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join("\n");
const keys=["NVIDIA","ORACLE","COREWEAVE","BROADCOM","AMD","MICROSOFT","META","ALPHABET","AMAZON","SOFTBANK","DELL","INTEL"];
function payload() {
  return {
    schema_version:2,data_policy:"direct-reported-only",updated:"2026-10-19T12:00:00Z",
    coverage:{start:"2025-10-01",end:"2026-10-19",last_successful_file_date:"2026-10-16",files:[],failed_files:[],missing_files:[]},
    entities:Object.fromEntries(keys.map(k=>[k,{series:[],last:null,last_date:null,trades:0,last_trade_timestamp:null,last_report_timestamp:null,last_report_delay_seconds:null,recent_reports:[],latest_report:null,quality_counts:{}}])),
    methodology_details:{label:"Verified direct reports only"}
  };
}
async function boot(data,{status=200}={}) {
  const elements=new Map();
  function element(id) {
    if(!elements.has(id))elements.set(id,{textContent:"",innerHTML:"",className:"",dataset:{},addEventListener(){},getBoundingClientRect(){return {left:0,width:1000};}});
    return elements.get(id);
  }
  const context=vm.createContext({
    document:{getElementById:element,querySelectorAll(){return [];}},window:{addEventListener(){}},
    URL,Date,console,setInterval(){},fetch:async()=>({ok:status>=200 && status<300,status,json:async()=>data})
  });
  vm.runInContext(source,context,{filename:"dashboard-inline.js"});
  await new Promise(resolve=>setImmediate(resolve));
  return {context,e:element,run:js=>vm.runInContext(js,context),all:()=>[...elements.values()].map(e=>e.innerHTML+e.textContent).join("\n")};
}
async function main() {
  const empty=await boot(payload());
  assert.match(empty.e("feed-status").textContent,/검증된 직접 보고 스프레드 없음/);
  assert.equal(empty.e("last").textContent,"—");
  assert.equal((empty.e("peers").innerHTML.match(/<tr data-key=/g)||[]).length,12,"Null watchlist members must remain visible");
  assert.match(empty.e("peers").innerHTML,/CRWV/);
  assert.match(empty.e("peers").innerHTML,/Dell Inc/);
  assert.doesNotMatch(empty.all(),/NaN|Infinity/);
  const legacy=await boot({updated:"2026-10-19T12:00:00Z",entities:{NVIDIA:{last:500,series:[["2026-10-16",500,1]]}}});
  assert.match(legacy.e("feed-status").textContent,/DATA ERROR.*스키마 v2/);
  assert.equal(legacy.e("last").textContent,"—","Legacy estimated prices must never be displayed");
  const httpError=await boot(payload(),{status:404});
  assert.match(httpError.e("feed-status").textContent,/HTTP 404/);
  assert.equal(httpError.e("peers").innerHTML,"");

  const p=payload(), nv=p.entities.NVIDIA;
  Object.assign(nv,{series:[["2026-10-01",95,2],["2026-10-16",101,1]],last:101,last_date:"2026-10-16",trades:1,last_trade_timestamp:"2026-10-16T14:20:00Z",last_report_timestamp:"2026-10-16T15:20:00Z",last_report_delay_seconds:3600});
  p.entities.ORACLE.series=[["2026-10-15",110,1]];Object.assign(p.entities.ORACLE,{last:110,last_date:"2026-10-15",trades:1,last_trade_timestamp:"2026-10-15T12:00:00Z"});
  const observed=await boot(p);
  assert.equal(observed.e("last").textContent,"101.0");
  assert.match(observed.e("median-date").textContent,/2026-10-16.*대상 보고 1건.*개별 최신 체결가 아님/);
  assert.equal((observed.e("chart").innerHTML.match(/<circle /g)||[]).length,2);
  assert.doesNotMatch(observed.e("chart").innerHTML,/<path |<polyline /,"Missing dates must have no interpolating price line");
  assert.doesNotMatch(observed.e("peers").innerHTML,/1D|VS NVDA|\+9\.0/);
  assert.doesNotMatch(observed.all(),/NaN|Infinity/);
  observed.run('showError(new Error("fixture refresh failed"))');
  assert.equal(observed.e("last").textContent,"—","A failed refresh must remove prior numeric display");
  assert.equal(observed.e("chart").innerHTML,"");

  const raw=payload();
  const report={report_id:"<img src=x onerror=alert(1)>",entity_name:"NVIDIA CORP",execution_timestamp:"2026-10-16T14:20:00Z",report_timestamp:"2026-10-16T15:20:00Z",maturity_date:"2031-09-20",remaining_years:4.93,currency:"USD",spread_bp:null,quality_reasons:["unverified_spread_notation"],spread_legs:[{leg:1,raw:"0.0095",notation:"3",currency:"USD"}],fixed_rates:[{leg:1,raw:"0.01"}],upfront:{amount:"10000",type:"UFRO"},source_files:[{date:"2026-10-16",url:"javascript:alert(1)"}]};
  raw.entities.NVIDIA.recent_reports=[report];raw.entities.NVIDIA.latest_report=report;
  raw.entities.NVIDIA.quality_counts={unverified_spread_notation:1};
  raw.entities.NVIDIA.scope_exclusion_counts={outside_4_6_year_bucket:22,not_usd:3};
  raw.coverage.failed_files=[{date:"2026-10-19",error:"<script>bad()</script>"}];
  const unverified=await boot(raw);
  assert.match(unverified.e("raw-summary").innerHTML,/0\.0095.*notation 3/);
  assert.match(unverified.e("entity-reason").textContent,/원천 spread 필드는 존재하지만 검증된 BP 값 없음/);
  assert.doesNotMatch(unverified.e("entity-reason").textContent,/unverified_spread_notation|outside_4_6_year_bucket|22/);
  assert.ok(unverified.e("entity-reason").textContent.length<100,"Quote explanation must remain compact");
  assert.match(unverified.e("reason-details").innerHTML,/^<details><summary>제외 사유·계약조건 집계<\/summary>/,"Exclusion counts must be collapsed by default");
  assert.match(unverified.e("reason-details").innerHTML,/unverified_spread_notation.*1건/);
  assert.match(unverified.e("reason-details").innerHTML,/outside_4_6_year_bucket.*22건/);
  assert.match(unverified.e("reason-details").innerHTML,/not_usd.*3건/);
  assert.equal(unverified.e("last").textContent,"—");
  assert.doesNotMatch(unverified.e("records").innerHTML,/<img|href="javascript:/);
  assert.match(unverified.e("records").innerHTML,/&lt;img/);
  assert.doesNotMatch(unverified.e("coverage-details").innerHTML,/<script>/);
  assert.match(unverified.e("feed-status").textContent,/수집 범위 불완전/);
  const historical=payload();
  const latestCoupon={...report,report_id:"recent-coupon-only",execution_timestamp:"2026-10-16T15:21:52Z",spread_legs:[{leg:1,raw:null,notation:null}],quality_reasons:["no_reported_spread"]};
  const oldSpread={...report,report_id:"old-spread-report",execution_timestamp:"2026-04-14T13:08:41Z",spread_legs:[{leg:1,raw:"0.000125",notation:"3"}]};
  historical.entities.COREWEAVE.latest_report=latestCoupon;
  historical.entities.COREWEAVE.latest_spread_report=oldSpread;
  historical.entities.COREWEAVE.recent_reports=[latestCoupon];
  const olderRaw=await boot(historical);
  olderRaw.run('selected="COREWEAVE";render()');
  assert.match(olderRaw.e("raw-summary").innerHTML,/recent-coupon-only/);
  assert.match(olderRaw.e("raw-summary").innerHTML,/old-spread-report/);
  assert.match(olderRaw.e("raw-summary").innerHTML,/0\.000125.*notation 3/);
  assert.match(olderRaw.e("raw-summary").innerHTML,/2026-04-14 13:08:41Z/);
  assert.equal(olderRaw.e("last").textContent,"—","Historical raw field must remain unconverted");
  const badNumber=payload();badNumber.entities.NVIDIA.series=[["2026-10-16",Infinity,1]];
  assert.match((await boot(badNumber)).e("feed-status").textContent,/DATA ERROR/);
  const mismatch=payload();mismatch.entities.NVIDIA.last=50;
  assert.match((await boot(mismatch)).e("feed-status").textContent,/DATA ERROR/);
  if(process.argv[2]){
    const real=JSON.parse(fs.readFileSync(process.argv[2],"utf8"));
    const actual=await boot(real);
    assert.doesNotMatch(actual.e("feed-status").textContent,/DATA ERROR/,actual.e("feed-status").textContent);
    for(const key of keys){actual.run(`selected=${JSON.stringify(key)};render()`);assert.doesNotMatch(actual.all(),/NaN|Infinity/);}
    console.log("PASS: current v2 payload renders all 12 watchlist members");
  }
  console.log("PASS: all-null, legacy refusal, HTTP error, sparse chart, differing dates, stale refresh, raw unverified evidence including full-history latest_spread_report, HTML escaping, invalid numeric/schema data");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
