/* Minimal in-browser xlsx reader for the data-update feature.
 *
 * xlsx is a zip of XML files. We use the browser's built-in CompressionStreams
 * API for deflate decompression and DOMParser for XML parsing. No external
 * dependency — the report stays self-contained.
 *
 * Supports the common case: one sheet, inline strings or sharedStrings,
 * string/number/boolean/inline-str cell types. Does NOT support formulas,
 * styled cells, charts, images, multi-sheet — those would need SheetJS.
 */
(function(global){
  'use strict';
  function log(){if(global.console)console.warn.apply(console,['[xlsx-reader]'].concat([].slice.call(arguments)));}
  // ---- ZIP reader ----
  function findSignature(buffer,signature,start){
    var limit=buffer.byteLength-signature.length;
    for(var i=start||0;i<=limit;i++){
      var match=true;
      for(var j=0;j<signature.length;j++){if(buffer[i+j]!==signature[j]){match=false;break;}}
      if(match)return i;
    }
    return -1;
  }
  function readBuffer(arrayBuffer){return new Uint8Array(arrayBuffer);}
  function ascii(buffer,start,length){var out='';for(var i=0;i<length;i++)out+=String.fromCharCode(buffer[start+i]);return out;}
  function readUInt16LE(buffer,offset){return buffer[offset]|(buffer[offset+1]<<8);}
  function readUInt32LE(buffer,offset){return (buffer[offset]|(buffer[offset+1]<<8)|(buffer[offset+2]<<16)|(buffer[offset+3]<<24))>>>0;}
  function parseZipEntries(buffer){
    var eocdSig=new Uint8Array([0x50,0x4b,0x05,0x06]);
    var eocdIdx=findSignature(buffer,eocdSig);
    if(eocdIdx<0)throw new Error('ZIP 中央目录未找到');
    var entryCount=readUInt16LE(buffer,eocdIdx+10);
    var cdStart=readUInt32LE(buffer,eocdIdx+16);
    var entries=[];
    var offset=cdStart;
    var localSig=new Uint8Array([0x50,0x4b,0x07,0x08]);
    for(var i=0;i<entryCount;i++){
      if(readUInt32LE(buffer,offset)!==0x02014b50)throw new Error('ZIP 中央目录头格式错误');
      var compMethod=readUInt16LE(buffer,offset+10);
      var compSize=readUInt32LE(buffer,offset+20);
      var uncompSize=readUInt32LE(buffer,offset+24);
      var nameLen=readUInt16LE(buffer,offset+28);
      var extraLen=readUInt16LE(buffer,offset+30);
      var commentLen=readUInt16LE(buffer,offset+32);
      var localHeaderOffset=readUInt32LE(buffer,offset+42);
      var name=ascii(buffer,offset+46,nameLen);
      entries.push({name:name,compMethod:compMethod,compSize:compSize,uncompSize:uncompSize,localHeaderOffset:localHeaderOffset});
      offset+=46+nameLen+extraLen+commentLen;
    }
    return entries;
  }
  function inflate(buffer){
    return new Response(buffer).arrayBuffer().then(function(buf){return new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).arrayBuffer();});
  }
  function readZipEntry(buffer,entry){
    var localSig=new Uint8Array([0x50,0x4b,0x03,0x04]);
    var lh=findSignature(buffer,localSig,entry.localHeaderOffset);
    if(lh<0)throw new Error('本地 ZIP 头未找到：'+entry.name);
    var nameLen=readUInt16LE(buffer,lh+26);
    var extraLen=readUInt16LE(buffer,lh+28);
    var dataStart=lh+30+nameLen+extraLen;
    var data=buffer.slice(dataStart,dataStart+entry.compSize);
    if(entry.compMethod===0)return Promise.resolve(data);
    if(entry.compMethod===8)return inflate(data).then(function(ab){return new Uint8Array(ab);});
    throw new Error('不支持的压缩方式：'+entry.compMethod);
  }
  // ---- XML helpers ----
  function parseXml(text){
    if(text && text.charCodeAt(0)===0xFEFF)text=text.slice(1); // strip UTF-8 BOM
    var parser=new DOMParser();
    var doc=parser.parseFromString(text,'application/xml');
    if(doc.getElementsByTagName('parsererror').length)throw new Error('XML 解析失败');
    return doc;
  }
  function getElementsByLocalName(parent,name){
    // Match by LOCAL name so prefixed XML (<x:sheet>, <x:c>, <x:v>…) from any
    // xlsx generator works, not just unprefixed/default-namespace files.
    var out=[],nodes=null;
    try{ nodes=parent.getElementsByTagNameNS('*',name); }catch(e){ nodes=null; }
    if(!nodes||!nodes.length){ nodes=parent.getElementsByTagName(name); }
    if(nodes){ for(var i=0;i<nodes.length;i++)out.push(nodes[i]); }
    return out;
  }
  // ---- shared strings ----
  function loadSharedStrings(xmlText){
    if(!xmlText)return [];
    var doc=parseXml(xmlText);
    var items=[];var sis=getElementsByLocalName(doc,'si');
    sis.forEach(function(si){
      var text='';
      var ts=getElementsByLocalName(si,'t');
      ts.forEach(function(t){text+=t.textContent;});
      items.push(text);
    });
    return items;
  }
  // ---- sheet ----
  // We parse <row>/<c> with optional type. Inline strings use <is><t>...</t></is>.
  // Excel DATES are numeric cells (t="n") whose style's numFmtId is a date
  // format — the raw <v> is a serial number (45123), which used to reach the
  // report verbatim: time axes went NULL, upsert primary keys mismatched.
  // We now decode date-styled numerics to ISO 'YYYY-MM-DD' (styles.xml).
  var DATE_BUILTIN_NUmfmts={14:1,15:1,16:1,17:1,22:1,27:1,28:1,29:1,30:1,31:1,32:1,33:1,34:1,35:1,36:1,50:1,51:1,52:1,53:1,54:1,55:1,56:1,57:1,58:1};
  function serialToISO(text){
    var n=Number(text);
    if(!isFinite(n)||n<=0)return null;
    // Excel 1900 system: serial 25569 = 1970-01-01; serials ≤59 predate the
    // phantom 1900-02-29 and shift by one day (standard SheetJS formula).
    var days=Math.floor(n)+(n<=59?1:0);
    var d=new Date((days-25569)*86400000);
    if(isNaN(d.getTime()))return null;
    return d.toISOString().slice(0,10);
  }
  function loadDateStyles(stylesText){
    // xf index → date? Built-in date numFmtIds, or a custom numFmt whose
    // formatCode carries a year/day token (time-only codes are excluded —
    // an h:mm cell is not a calendar date).
    var map=[];
    if(!stylesText)return map;
    try{
      var doc=parseXml(stylesText);
      var customs={};
      getElementsByLocalName(doc,'numFmt').forEach(function(nf){
        var code=nf.getAttribute('formatCode')||'';
        var unquoted=code.replace(/"[^"]*"/g,'').replace(/\\./g,'');
        if(/[yd]/i.test(unquoted))customs[parseInt(nf.getAttribute('numFmtId')||'0',10)]=true;
      });
      var xfs=null;
      getElementsByLocalName(doc,'cellXfs').forEach(function(cx){xfs=cx;});
      if(xfs){
        getElementsByLocalName(xfs,'xf').forEach(function(xf){
          var id=parseInt(xf.getAttribute('numFmtId')||'0',10);
          map.push(!!(DATE_BUILTIN_NUmfmts[id]||customs[id]));
        });
      }
    }catch(e){log('styles.xml 解析失败，日期格将保持序列号：',e&&e.message);}
    return map;
  }
  function cellType(c){
    var t=c.getAttribute('t')||'n';
    return t==='str'?'str':t==='inlineStr'?'inlineStr':t==='s'?'s':t==='b'?'b':'n';
  }
  function decodeCell(c,sharedStrings,dateStyles){
    var vEl=getElementsByLocalName(c,'v')[0];
    var isEl=getElementsByLocalName(c,'is')[0];
    var type=cellType(c);
    var raw;
    if(type==='inlineStr'&&isEl){var tEl=getElementsByLocalName(isEl,'t')[0];raw=tEl?tEl.textContent:'';}
    else if(type==='s'){raw=sharedStrings[parseInt(vEl?vEl.textContent:'0',10)]||'';}
    else if(vEl){raw=vEl.textContent;}
    else return '';
    if(type==='b')return raw==='1'?'TRUE':'FALSE';
    if((type==='n')&&dateStyles&&dateStyles.length){
      var sIdx=parseInt(c.getAttribute('s')||'-1',10);
      if(sIdx>=0&&sIdx<dateStyles.length&&dateStyles[sIdx]){
        var iso=serialToISO(raw);
        if(iso)return iso;
      }
    }
    return raw;
  }
  function colIndexFromRef(ref){
    // 'A1' -> 0, 'B12' -> 1, 'AA3' -> 26
    var letters='';var i=0;
    while(i<ref.length&&ref.charAt(i)>='A'&&ref.charAt(i)<='Z'){letters+=ref.charAt(i);i++;}
    var n=0;
    for(var k=0;k<letters.length;k++){n=n*26+(letters.charCodeAt(k)-64);}
    return n-1;
  }
  function loadSheet(sheetText,sharedStrings,dateStyles){
    var doc=parseXml(sheetText);
    var rows=[];
    var rowEls=getElementsByLocalName(doc,'row');
    // 行号兜底计数器：部分流式生成器不写 <row r>。r 缺失时旧代码算出 -1，
    // rows[-1] 是非索引属性、被下面的 filter 整体丢弃 → 整表误报「sheet 为空」。
    var nextRow=0;
    rowEls.forEach(function(row){
      var rAttr=parseInt(row.getAttribute('r')||'',10);
      var rowIndex=(isFinite(rAttr)&&rAttr>=1)?rAttr-1:nextRow;
      nextRow=rowIndex+1;
      var cells={};
      var cEls=getElementsByLocalName(row,'c');
      // 列号兜底计数器（与 nextRow 同一哲学：部分流式生成器不写 <c r>；ref 是
      // A1 式字母列，小写字母列也要归一——colIndexFromRef 只认大写，小写会算出
      // -1 把格悄悄丢进 cells[-1]，全表 r 缺失时整表塌成空阵误报「sheet 为空」）。
      var cellPos=0;
      cEls.forEach(function(c){
        var ref=c.getAttribute('r')||'';
        var col=ref?colIndexFromRef(ref.toUpperCase()):cellPos;
        cellPos=col+1;
        var value=decodeCell(c,sharedStrings,dateStyles);
        cells[col]=value;
      });
      var maxCol=Object.keys(cells).reduce(function(a,b){return Math.max(a,parseInt(b,10));},-1);
      var arr=[];for(var i=0;i<=maxCol;i++)arr.push(cells[i]!=null?cells[i]:'');
      rows[rowIndex]=arr;
    });
    return rows.filter(function(r){return r&&r.length>0;});
  }
  // ---- workbook sheet name -> file path ----
  function loadWorkbookRels(relsXml){
    var doc=parseXml(relsXml);
    var map={};
    var rels=getElementsByLocalName(doc,'Relationship');
    rels.forEach(function(rel){map[rel.getAttribute('Id')]=rel.getAttribute('Target');});
    return map;
  }
  function loadWorkbook(wbXml){
    var doc=parseXml(wbXml);
    var sheets=getElementsByLocalName(doc,'sheet');
    var out=[];
    sheets.forEach(function(s){
      out.push({name:s.getAttribute('name'),rid:s.getAttribute('r:id'),sheetId:s.getAttribute('sheetId')});
    });
    return out;
  }
  // ---- main entry ----
  async function readXlsx(file){
    var buf=readBuffer(await file.arrayBuffer());
    var entries=parseZipEntries(buf);
    var byName={};
    entries.forEach(function(entry){byName[entry.name]=entry;});
    if(!byName['xl/workbook.xml'])throw new Error('不是有效的 xlsx 文件');
    function entryToText(entry){return readZipEntry(buf,entry).then(function(u8){return new TextDecoder('utf-8').decode(u8);});}
    var wbText=await entryToText(byName['xl/workbook.xml']);
    var sheets=loadWorkbook(wbText);
    if(!sheets.length)throw new Error('xlsx 不包含任何 sheet');
    var relsText=byName['xl/_rels/workbook.xml.rels']?await entryToText(byName['xl/_rels/workbook.xml.rels']):'';
    var rels=relsText?loadWorkbookRels(relsText):{};
    var sharedStrings=byName['xl/sharedStrings.xml']?loadSharedStrings(await entryToText(byName['xl/sharedStrings.xml'])):[];
    var stylesEntry=byName['xl/styles.xml'];
    var dateStyles=stylesEntry?loadDateStyles(await entryToText(stylesEntry)):[];
    var first=sheets[0];
    var target=(rels[first.rid]||'worksheets/sheet1.xml').replace(/^\/+/,'');
    if(!/^xl\//.test(target))target='xl/'+target;
    var sheetEntry=byName[target];
    if(!sheetEntry)throw new Error('xlsx 找不到 sheet1.xml（'+target+'）');
    var sheetText=await entryToText(sheetEntry);
    var rows=loadSheet(sheetText,sharedStrings,dateStyles);
    if(!rows.length)throw new Error('sheet 为空');
    var header=rows[0]||[];
    var body=rows.slice(1);
    return {columns:header.map(function(c){return String(c);}),rows:body};
  }
  global.AIReportReadXlsx=readXlsx;
})(typeof window!=='undefined'?window:this);