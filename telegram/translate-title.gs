// Public-title translation only. No Gmail, Drive, blog, or Telegram access.
function doGet(e) {
  if (e && e.parameter && typeof e.parameter.title === 'string') return doPost({postData:{contents:JSON.stringify({title:e.parameter.title})}});
  return json_({ok:true, service:'hero-news-title-translation', version:2});
}
function json_(value) { return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON); }
function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    if (!e || !e.postData || e.postData.contents.length > 4000) return json_({ok:false,error:'invalid_request'});
    var input=JSON.parse(e.postData.contents), title=input.title;
    if (typeof title !== 'string' || !title.trim() || title.length > 500 || /[\r\n]/.test(title)) return json_({ok:false,error:'invalid_title'});
    if (!lock.tryLock(10000)) return json_({ok:false,error:'busy'});
    var props=PropertiesService.getScriptProperties();
    var key='t_v3_'+Utilities.base64EncodeWebSafe(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,title));
    var cached=props.getProperty(key);
    if (cached) return json_({ok:true,title:JSON.parse(cached).text,cached:true});
    var now=Date.now(), quota=JSON.parse(props.getProperty('quota') || '{"since":0,"count":0}');
    if (now-quota.since >= 86400000) quota={since:now,count:0};
    if (quota.count >= 100) return json_({ok:false,error:'daily_limit'});
    quota.count++;props.setProperty('quota',JSON.stringify(quota));
    var names=[['仮面ライダーギーツ','Kamen Rider Geats'],['ウルトラセブン','Ultraseven'],['ウルトラマン','Ultraman'],['仮面ライダー','Kamen Rider'],['スーパー戦隊','Super Sentai'],['ゴジラ','Godzilla'],['ガメラ','Gamera'],['スパイダーマン','Spider-Man'],['アイアンマン','Iron Man'],['バットマン','Batman'],['スーパーマン','Superman'],['マーベル','Marvel']];
    var protectedNames=[];
    var prepared=title;
    names.forEach(function(pair){ if(prepared.indexOf(pair[0])>=0) {var marker='ZXQNAME'+protectedNames.length+'QXZ';prepared=prepared.split(pair[0]).join(marker);protectedNames.push([marker,pair[1]]);} });
    var translated=LanguageApp.translate(prepared,'ja','en');
    protectedNames.forEach(function(pair){if(translated.indexOf(pair[0])<0)throw new Error('name_marker');translated=translated.split(pair[0]).join(pair[1]);});
    translated=translated.trim();
    if (!translated || translated.length>1500 || /[\r\n\u3040-\u30ff\u3400-\u9fff]/.test(translated)) return json_({ok:false,error:'translation_validation'});
    var all=props.getProperties();
    var keys=Object.keys(all).filter(function(k){return k.indexOf('t_')===0;});
    if(keys.length>=100) {keys.sort(function(a,b){return JSON.parse(all[a]).at-JSON.parse(all[b]).at;});keys.slice(0,25).forEach(function(k){props.deleteProperty(k);});}
    props.setProperty(key,JSON.stringify({text:translated,at:now}));
    return json_({ok:true,title:translated,cached:false});
  } catch (_) { return json_({ok:false,error:'translation_unavailable'}); }
  finally { if(lock.hasLock()) lock.releaseLock(); }
}
