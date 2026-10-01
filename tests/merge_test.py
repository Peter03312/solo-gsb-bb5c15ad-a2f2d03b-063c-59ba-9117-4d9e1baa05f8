"""重复失物记录合并：端到端 + 并发测试（标准库）。

前置：服务已运行在 http://127.0.0.1:8181，数据目录为 /tmp/lfdata。
覆盖：合并入口与校验、两份各有获选拒绝、合计一份获选（保留交还码、其余未获选）、
申请与改判记录归属、原凭证查询、并入物品公开消失/旧详情指向、不能恢复/再申请、
工作人员追溯（理由/原物品/时间）、访客不可见，以及与选定/改判/撤下/核码/另一合并并发。
"""
import http.cookiejar
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8181"
DB_PATH = "/tmp/lfdata/lostfound.db"
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def client():
    cj = http.cookiejar.CookieJar()
    return cj, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _jar_of(op):
    for h in op.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return http.cookiejar.CookieJar()


def req(op, method, path, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        resp = op.open(r)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def raw(op, method, path, form):
    """不跟随 303，返回 (status, body, location)。"""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    opener = urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPCookieProcessor(_jar_of(op))
    )
    try:
        resp = opener.open(r)
        return resp.status, resp.read().decode(), resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Location", "")


def raw_cookie(cookie_header, path, form):
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


def parallel2(fn_a, fn_b):
    """用两条全新的守护线程同时执行 fn_a/fn_b（各自共享一次性栅栏）。

    不复用线程池工作线程，避免上一个并发块的线程仍停在旧 Barrier 上导致死锁。
    """
    gate = threading.Barrier(2)
    results: dict[str, object] = {}

    def run(key, fn):
        gate.wait()
        results[key] = fn()

    ta = threading.Thread(target=run, args=("a", fn_a), daemon=True)
    tb = threading.Thread(target=run, args=("b", fn_b), daemon=True)
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    return results["a"], results["b"]


def submit_claim(item_id, answer, contact):
    _, v = client()
    st, body = req(v, "POST", f"/items/{item_id}/claims", form={"answer": answer, "contact": contact})
    assert st == 200 and "查询凭证" in body, st
    token = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()
    return token


def query(token):
    _, v = client()
    st, body = req(v, "GET", f"/query?token={urllib.parse.quote(token)}")
    assert st == 200
    return body


def new_item(pub, priv="私密特征"):
    st, _, loc = raw(staff, "POST", "/staff/items",
                     form={"csrf_token": SCSRF, "public_description": pub, "private_features": priv})
    assert st == 303, (st, loc)
    return int(loc.rsplit("/", 1)[1])


def claims_of(item_id):
    return [r[0] for r in db.execute(
        "SELECT id FROM claims WHERE item_id=? ORDER BY id", (item_id,)).fetchall()]


jar, staff = client()
_, body = req(staff, "GET", "/staff/login")
req(staff, "POST", "/staff/login", form={"csrf_token": csrf(body), "password": "secret123"})
_, dash = req(staff, "GET", "/staff")
SCSRF = csrf(dash)
cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)

db = sqlite3.connect(DB_PATH)

# --------------------------------------------------------------- 场景一：合计一份获选
print("== 准备两条重复记录（保留 K1 有获选；并入 S1 有待核对） ==")
k1 = new_item("重复-黑色双肩包-保留", "私密K1-内袋姓名贴")
s1 = new_item("重复-黑色双肩包-误登记", "私密S1-挂件小熊")

tK = submit_claim(k1, "保留物获选答案", "contact-K")
tL = submit_claim(k1, "保留物陪跑答案", "contact-L")
tS = submit_claim(s1, "并入物待核对答案", "contact-S")
tS2 = submit_claim(s1, "并入物另一待核对", "contact-S2")
cK, cL = claims_of(k1)[-2:]
cS, cS2 = claims_of(s1)[-2:]

st, _, loc = raw(staff, "POST", f"/staff/claims/{cK}/select", form={"csrf_token": SCSRF})
assert st == 303, st
codeK = re.search(r'codebox">(\d{8})', query(tK)).group(1)

print("== 合并入口与表单 ==")
st, body = req(staff, "GET", "/staff/items/merge")
check("合并表单可打开", st == 200 and "合并重复失物记录" in body and f'value="{k1}"' in body
      and f'value="{s1}"' in body)
st, body = req(staff, "GET", f"/staff/items/merge?item={s1}")
check("从并入物品页带入预选", f'name="source_item_id"' in body)
st, body = req(staff, "GET", f"/staff/items/{s1}")
check("保管中物品页提供合并入口", f'/staff/items/merge?item={s1}' in body)

print("== 拒绝情形：不改动任何状态 ==")
st, _, _ = raw(staff, "POST", "/staff/items/merge",
               form={"csrf_token": "bad", "keep_item_id": k1,
                     "source_item_id": s1, "reason": "x"})
check("伪造 CSRF 被拒(400)", st == 400)

st, _, loc2 = raw(staff, "POST", "/staff/items/merge",
                  form={"csrf_token": SCSRF, "keep_item_id": k1, "source_item_id": k1,
                        "reason": "自身合并"})
check("保留与并入相同被拒并跳回", st == 303 and loc2 == "/staff/items/merge")
st, body = req(staff, "GET", "/staff/items/merge")
check("相同记录错误提示", "两条不同的记录" in body)

st, _, loc2 = raw(staff, "POST", "/staff/items/merge",
                  form={"csrf_token": SCSRF, "keep_item_id": k1, "source_item_id": s1,
                        "reason": "   "})
check("空理由被拒并带 item 返回", st == 303 and loc2 == f"/staff/items/merge?item={s1}")
st, body = req(staff, "GET", loc2)
check("空理由提示", "合并理由不能为空" in body)
check("空理由后两条均仍保管",
      db.execute("SELECT status FROM items WHERE id=?", (s1,)).fetchone()[0] == "kept"
      and db.execute("SELECT status FROM items WHERE id=?", (k1,)).fetchone()[0] == "kept")

st, _, _ = raw(staff, "POST", "/staff/items/merge",
               form={"csrf_token": SCSRF, "keep_item_id": 99999901,
                     "source_item_id": s1, "reason": "幽灵保留"})
check("保留物品不存在不产生 500", st in (303, 404), str(st))
st, _, _ = raw(staff, "POST", "/staff/items/merge",
               form={"csrf_token": SCSRF, "keep_item_id": k1,
                     "source_item_id": 99999902, "reason": "幽灵并入"})
check("并入物品不存在不产生 500", st in (303, 404), str(st))

# 撤下的记录不能参与合并
st, _, loc2 = raw(staff, "POST", "/staff/items",
                  form={"csrf_token": SCSRF, "public_description": "待撤下重复", "private_features": "z"})
rm = int(loc2.rsplit("/", 1)[1])
raw(staff, "POST", f"/staff/items/{rm}/remove", form={"csrf_token": SCSRF})
st, _, _ = raw(staff, "POST", "/staff/items/merge",
               form={"csrf_token": SCSRF, "keep_item_id": k1, "source_item_id": rm,
                     "reason": "并入已撤下"})
st, body = req(staff, "GET", f"/staff/items/merge?item={rm}")
check("并入已撤下记录被拒", "都必须仍在保管中" in body)
check("已撤下记录状态不变",
      db.execute("SELECT status FROM items WHERE id=?", (rm,)).fetchone()[0] == "removed")

print("== 正常合并（合计一份获选） ==")
st, _, loc2 = raw(staff, "POST", "/staff/items/merge",
                  form={"csrf_token": SCSRF, "keep_item_id": k1, "source_item_id": s1,
                        "reason": "核对确认同一双肩包，第二条重复录入"})
check("合并成功跳转到保留物品", st == 303 and loc2 == f"/staff/items/{k1}", f"{st} {loc2}")
st, body = req(staff, "GET", loc2)
check("跳转页提示合并完成", "已合并" in body)

check("并入物品标记 merged 并指向保留物品",
      db.execute("SELECT status,merged_into_item_id FROM items WHERE id=?", (s1,)).fetchone()
      == ("merged", k1))
check("保留物品仍保管中",
      db.execute("SELECT status FROM items WHERE id=?", (k1,)).fetchone()[0] == "kept")

moved = {r[0]: (r[1], r[2]) for r in db.execute(
    "SELECT id,item_id,status FROM claims WHERE id IN (?,?,?,?)",
    (cK, cL, cS, cS2)).fetchall()}
check("并入物品的申请归到保留物品",
      moved[cS][0] == k1 and moved[cS2][0] == k1, str(moved))
check("获选申请仍获选", moved[cK] == (k1, "selected"), str(moved))
check("保留物其他待核对标记未获选", moved[cL] == (k1, "rejected"), str(moved))
check("并入物待核对申请均标记未获选",
      moved[cS] == (k1, "rejected") and moved[cS2] == (k1, "rejected"), str(moved))
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                  (k1,)).fetchone()[0]
check("保留物品恰有一份获选", nsel == 1, str(nsel))

mg = db.execute("SELECT keep_item_id,source_item_id,reason FROM item_merges WHERE source_item_id=?",
                (s1,)).fetchone()
check("合并记录保存双方与理由",
      mg == (k1, s1, "核对确认同一双肩包，第二条重复录入"), str(mg))
check("合并记录含时间",
      db.execute("SELECT created_at FROM item_merges WHERE source_item_id=?", (s1,)).fetchone()[0]
      is not None)

print("== 交还码保留且可核销；已作废码仍不能核销 ==")
# 获选人的码原样保留（claim 改了 item_id，码仍挂在 claim 上）
active = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc WHERE hc.claim_id=? "
    "AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL", (cK,)).fetchone()[0]
check("获选交还码保留为有效", active == 1, str(active))
body = query(tK)
check("获选人凭证仍查到本人申请与交还码",
      "已获选" in body and codeK in body and "重复-黑色双肩包-保留" in body)

# 已作废（invalidated）码核销仍被拒
raw(staff, "POST", f"/staff/claims/{cK}/resend", form={"csrf_token": SCSRF})  # 旧码作废、发新码
codeK2 = re.search(r'codebox">(\d{8})', query(tK)).group(1)
st, body = req(staff, "GET", "/staff/verify")
st, body = req(staff, "POST", "/staff/verify", form={"csrf_token": csrf(body), "code": codeK})
check("合并前已作废的旧码仍不能核销", "已失效" in body)
check("物品未因旧码被交还",
      db.execute("SELECT status FROM items WHERE id=?", (k1,)).fetchone()[0] == "kept")

print("== 原查询凭证继续查询本人申请（归属已变） ==")
bq = query(tS)
check("并入物原申请人凭证可查、显示未获选",
      "未获选" in bq and 'class="codebox"' not in bq)
check("查询页显示保留物品的公开描述", "重复-黑色双肩包-保留" in bq)
check("查询页不泄露任何私密特征", "私密K1" not in bq and "私密S1" not in bq)
check("不泄露他人答案/联系方式", "contact-K" not in bq and "保留物获选答案" not in bq)

print("== 并入物品公开列表消失、旧详情指向保留物品 ==")
_, v = client()
st, body = req(v, "GET", "/")
check("并入物品不在公开列表", "重复-黑色双肩包-误登记" not in body)
check("保留物品仍在公开列表", "重复-黑色双肩包-保留" in body)
st, body = req(v, "GET", f"/items/{s1}")
check("旧详情可打开并指向保留物品",
      st == 200 and "已并入保留记录" in body and f'/items/{k1}' in body
      and "重复-黑色双肩包-保留" in body)
check("旧详情不显示并入理由与私密特征",
      "第二条重复录入" not in body and "私密S1" not in body and "私密K1" not in body)
st, body = req(v, "POST", f"/items/{s1}/claims", form={"answer": "a", "contact": "b"})
check("并入物品不能再收申请", st == 400 and "并入" in body)

print("== 不能恢复、不能再合并/撤下 ==")
# 不存在恢复路由对 merged 生效；直接验证 restore 条件更新 0 行，状态仍 merged
st, _, _ = raw(staff, "POST", f"/staff/items/{s1}/restore", form={"csrf_token": SCSRF})
check("对并入物品调用恢复不报错但状态不变",
      db.execute("SELECT status FROM items WHERE id=?", (s1,)).fetchone()[0] == "merged")
st, body, _ = raw(staff, "POST", f"/staff/items/{s1}/remove", form={"csrf_token": SCSRF})
check("撤下并入物品被业务拒绝(400)且不改状态",
      st == 400 and "已并入其他记录，不能撤下" in body, str(st))
st, body = req(staff, "GET", f"/staff/items/{s1}")
check("并入物品撤下拒绝后仍指向保留物品", f'/staff/items/{k1}' in body)
check("撤下尝试后仍为 merged",
      db.execute("SELECT status FROM items WHERE id=?", (s1,)).fetchone()[0] == "merged")
st, _, locm = raw(staff, "POST", "/staff/items/merge",
                 form={"csrf_token": SCSRF, "keep_item_id": k1, "source_item_id": s1,
                       "reason": "再合并一次"})
check("再次合并被拒并跳回表单", st == 303 and locm == f"/staff/items/merge?item={s1}")
st, body = req(staff, "GET", locm)
check("已并入记录不能再次合并", "都必须仍在保管中" in body)
st, _, locm2 = raw(staff, "POST", "/staff/items/merge",
                  form={"csrf_token": SCSRF, "keep_item_id": s1, "source_item_id": k1,
                        "reason": "反向再合并"})
check("并入记录作为保留物品被拒并跳回表单",
      st == 303 and locm2 == f"/staff/items/merge?item={k1}", str(st))
check("并入记录不能作为保留物品",
      db.execute("SELECT status FROM items WHERE id=?", (k1,)).fetchone()[0] == "kept"
      and db.execute(
          "SELECT COUNT(*) FROM item_merges WHERE keep_item_id=? AND source_item_id=?",
          (s1, k1)).fetchone()[0] == 0
      and db.execute(
          "SELECT COUNT(*) FROM item_merges WHERE keep_item_id=? AND source_item_id=?",
          (k1, s1)).fetchone()[0] == 1)

print("== 工作人员追溯（理由、原物品、时间） ==")
st, body = req(staff, "GET", f"/staff/items/{k1}")
check("保留物品页展示合并记录",
      "重复记录合并记录" in body and f"#{s1}" in body and "第二条重复录入" in body
      and "私密S1-挂件小熊" in body)
st, body = req(staff, "GET", f"/staff/items/{s1}")
check("并入物品工作人员页指向保留物品",
      "本记录为重复登记的并入物品" in body and f'/staff/items/{k1}' in body)
check("并入物品页无撤下/恢复/合并按钮",
      "/remove" not in body and "/restore" not in body and "/staff/items/merge" not in body)
_, vv = client()
st, body = req(vv, "GET", f"/staff/items/{k1}")
check("访客看不到合并记录(403)", st == 403)

# 先核销完成交还，确认合并后整链可用
st, body = req(staff, "GET", "/staff/verify")
st, _, loc2 = raw(staff, "POST", "/staff/verify", form={"csrf_token": csrf(body), "code": codeK2})
check("合并后新码核销成功，保留物品交还",
      st == 303 and loc2 == f"/staff/items/{k1}"
      and db.execute("SELECT status FROM items WHERE id=?", (k1,)).fetchone()[0] == "returned")

# --------------------------------------------------------------- 场景二：两份各有获选 -> 拒绝
print("== 两条各有获选时拒绝合且不改数据 ==")
a1 = new_item("双获选-A", "pa")
a2 = new_item("双获选-B", "pb")
ta1 = submit_claim(a1, "A获选", "cA1")
ta2 = submit_claim(a2, "B获选", "cA2")
ca1 = claims_of(a1)[-1]
ca2 = claims_of(a2)[-1]
raw(staff, "POST", f"/staff/claims/{ca1}/select", form={"csrf_token": SCSRF})
raw(staff, "POST", f"/staff/claims/{ca2}/select", form={"csrf_token": SCSRF})
snap = {
    "items": db.execute("SELECT id,status FROM items WHERE id IN (?,?)", (a1, a2)).fetchall(),
    "claims": db.execute("SELECT id,item_id,status FROM claims WHERE id IN (?,?)",
                         (ca1, ca2)).fetchall(),
}
st, _, loc2 = raw(staff, "POST", "/staff/items/merge",
                  form={"csrf_token": SCSRF, "keep_item_id": a1, "source_item_id": a2,
                        "reason": "双获选也合"})
check("双获选合并被拒(303 回表单带错误)", st == 303 and loc2 == f"/staff/items/merge?item={a2}")
st, body = req(staff, "GET", loc2)
check("双获选错误提示", "各有获选申请" in body)
check("拒绝后物品状态不变",
      db.execute("SELECT id,status FROM items WHERE id IN (?,?) ORDER BY id", (a1, a2)).fetchall()
      == snap["items"])
check("拒绝后申请归属/状态不变",
      db.execute("SELECT id,item_id,status FROM claims WHERE id IN (?,?) ORDER BY id",
                 (ca1, ca2)).fetchall() == snap["claims"])
check("未产生合并记录",
      db.execute("SELECT COUNT(*) FROM item_merges WHERE source_item_id=?", (a2,)).fetchone()[0] == 0)

# --------------------------------------------------------------- 场景三：两份均无获选 -> 待核对保留
print("== 两份均无获选：申请归并、待核对保持待核对 ==")
z1 = new_item("零获选-保留", "pz1")
z2 = new_item("零获选-并入", "pz2")
tz1 = submit_claim(z1, "Z1待核对", "cz1")
tz2 = submit_claim(z2, "Z2待核对", "cz2")
cz1 = claims_of(z1)[-1]
cz2 = claims_of(z2)[-1]
st, _, loc2 = raw(staff, "POST", "/staff/items/merge",
                  form={"csrf_token": SCSRF, "keep_item_id": z1, "source_item_id": z2,
                        "reason": "无人获选的重复登记"})
check("零获选合并成功", st == 303 and loc2 == f"/staff/items/{z1}")
rows = dict(db.execute("SELECT id,item_id||'|'||status FROM claims WHERE id IN (?,?)",
                       (cz1, cz2)).fetchall())
check("两份待核对申请均归保留物品且仍待核对",
      rows[cz1] == f"{z1}|pending" and rows[cz2] == f"{z1}|pending", str(rows))
check("零获选合并后仍可在保留物品选定并入来的申请",
      query(tz2).find("待核对") >= 0)
# 可选：直接选定并入来的待核对申请，验证无唯一约束冲突
st, _, _ = raw(staff, "POST", f"/staff/claims/{cz2}/select", form={"csrf_token": SCSRF})
check("可选定并入来的申请(303)", st == 303)
check("选定后保留物品恰一份获选",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                 (z1,)).fetchone()[0] == 1)
check("另一份待核对被连带标记未获选",
      db.execute("SELECT status FROM claims WHERE id=?", (cz1,)).fetchone()[0] == "rejected")

# --------------------------------------------------------------- 场景四：改判记录随并入物品迁移
print("== 并入物品的改判记录归到保留物品 ==")
r1 = new_item("带改判-保留", "pr1")
r2 = new_item("带改判-并入", "pr2")
submit_claim(r2, "R2先获选", "cr2a")
submit_claim(r2, "R2后获选", "cr2b")
rr = claims_of(r2)
r_from, r_to = rr[-2], rr[-1]
raw(staff, "POST", f"/staff/claims/{r_from}/select", form={"csrf_token": SCSRF})
raw(staff, "POST", f"/staff/claims/{r_from}/rejudge",
    form={"csrf_token": SCSRF, "to_claim_id": r_to, "reason": "并入前的改判"})
check("并入前改判记录挂在并入物品",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (r2,)).fetchone()[0] == 1)
raw(staff, "POST", "/staff/items/merge",
    form={"csrf_token": SCSRF, "keep_item_id": r1, "source_item_id": r2, "reason": "迁移改判链"})
check("改判记录迁移到保留物品",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (r1,)).fetchone()[0] == 1
      and db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (r2,)).fetchone()[0] == 0)
st, body = req(staff, "GET", f"/staff/items/{r1}")
check("保留物品页可见迁移来的改判记录", "并入前的改判" in body and "认领改判记录" in body)
# 迁移来的获选申请仍可再改判给同物品的未获选申请
rc = claims_of(r1)
check("并入来的申请都在保留物品下",
      all(db.execute("SELECT item_id FROM claims WHERE id=?", (x,)).fetchone()[0] == r1 for x in rc[-2:]))

# --------------------------------------------------------------- 场景五：并发
print("== 并发：合并与选定/改判/撤下/核码/另一合并 不产生双获选 ==")
# 5.1 合并 vs 选定：两条各有待核对，并发「把 S 并入 K」与「选定 S 的申请」
pk = new_item("并发-保留", "ppk")
ps = new_item("并发-并入", "pps")
submit_claim(pk, "PK申请", "cpk")
submit_claim(ps, "PS申请", "cps")
pk_c = claims_of(pk)[-1]
ps_c = claims_of(ps)[-1]


def merge_hit():
    return raw_cookie(cookie_header, "/staff/items/merge",
                      {"csrf_token": SCSRF, "keep_item_id": pk, "source_item_id": ps,
                       "reason": "并发合并"})


def select_hit(cid):
    return raw_cookie(cookie_header, f"/staff/claims/{cid}/select", {"csrf_token": SCSRF})


rm_merge, rm_select = parallel2(merge_hit, lambda: select_hit(ps_c))

sstat = db.execute("SELECT status FROM items WHERE id=?", (ps,)).fetchone()[0]
# 无论谁先提交，保留物品（pk）下 selected 至多 1，且不会出现跨物品双获选
sel_pk = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                    (pk,)).fetchone()[0]
sel_ps = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                    (ps,)).fetchone()[0]
check("合并 vs 选定：无双获选（selected 总数 <=1）",
      sel_pk + sel_ps <= 1, f"{rm_merge} {rm_select} merged={sstat} sel_pk={sel_pk} sel_ps={sel_ps}")
if sstat == "merged":
    check("合并先提交：并入申请归保留物，选定其后按新状态处理",
          db.execute("SELECT item_id FROM claims WHERE id=?", (ps_c,)).fetchone()[0] == pk
          and sel_pk <= 1)
else:
    check("选定先提交：合计一份获选，合并仍可成功并把获选申请随并入保留",
          sel_ps == 1 and sel_pk == 0)

# 5.2 两个合并并发（K<-S 与 S<-K 反向），只允许一个生效且不产生环
k2 = new_item("并发合并2-K", "q1")
s2i = new_item("并发合并2-S", "q2")


def merge_dir(keep, src):
    return raw_cookie(cookie_header, "/staff/items/merge",
                      {"csrf_token": SCSRF, "keep_item_id": keep, "source_item_id": src,
                       "reason": "并发反向合并"})


o1, o2 = parallel2(lambda: merge_dir(k2, s2i), lambda: merge_dir(s2i, k2))
stats = dict(db.execute("SELECT id,status FROM items WHERE id IN (?,?)", (k2, s2i)).fetchall())
n_merged = sum(1 for s in stats.values() if s == "merged")
n_merge_rows = db.execute(
    "SELECT COUNT(*) FROM item_merges WHERE source_item_id IN (?,?) AND keep_item_id IN (?,?)",
    (k2, s2i, k2, s2i)).fetchone()[0]
check("反向并发合并至多一条记录被并入（无环、无双向合并）",
      n_merged == 1 and n_merge_rows == 1, f"{o1} {o2} {stats} rows={n_merge_rows}")
# 保留物品必须 kept
keep_id_final = k2 if stats[k2] == "kept" else s2i
src_id_final = s2i if stats[k2] == "kept" else k2
check("最终保留物品仍保管中、并入物品指向它",
      stats[keep_id_final] == "kept"
      and db.execute("SELECT merged_into_item_id FROM items WHERE id=?",
                     (src_id_final,)).fetchone()[0] == keep_id_final)

# 5.3 合并 vs 撤下：并入物品并发被撤下时，合并必须失败
wk = new_item("并发撤下-K", "r1")
ws = new_item("并发撤下-S", "r2")
submit_claim(wk, "WK", "cwk")


def merge3():
    return raw_cookie(cookie_header, "/staff/items/merge",
                      {"csrf_token": SCSRF, "keep_item_id": wk, "source_item_id": ws,
                       "reason": "并发撤下"})


def remove3():
    return raw_cookie(cookie_header, f"/staff/items/{ws}/remove", {"csrf_token": SCSRF})


r_m, r_r = parallel2(merge3, remove3)
wstat = db.execute("SELECT status FROM items WHERE id=?", (ws,)).fetchone()[0]
check("合并 vs 撤下：并入物品不会既撤下又合并",
      wstat in ("removed", "merged"), f"{r_m} {r_r} {wstat}")
if wstat == "removed":
    check("撤下先提交：合并被拒，保留物品仍 kept 且无合并记录",
          db.execute("SELECT status FROM items WHERE id=?", (wk,)).fetchone()[0] == "kept"
          and db.execute("SELECT COUNT(*) FROM item_merges WHERE source_item_id=?",
                         (ws,)).fetchone()[0] == 0)

# 5.4 合并 vs 核码：各一份待核对，先在 S 选定获选，再并发「并入 K」与「核销 S 的码」
vk = new_item("并发核码-K", "v1")
vs = new_item("并发核码-S", "v2")
submit_claim(vk, "VK待核对", "cvk")
tv = submit_claim(vs, "VS获选", "cvs")
vs_c = claims_of(vs)[-1]
raw(staff, "POST", f"/staff/claims/{vs_c}/select", form={"csrf_token": SCSRF})
vcode = re.search(r'codebox">(\d{8})', query(tv)).group(1)


def merge4():
    return raw_cookie(cookie_header, "/staff/items/merge",
                      {"csrf_token": SCSRF, "keep_item_id": vk, "source_item_id": vs,
                       "reason": "并发核码"})


def verify4():
    return raw_cookie(cookie_header, "/staff/verify", {"csrf_token": SCSRF, "code": vcode})


rv_m, rv_v = parallel2(merge4, verify4)
kstat = db.execute("SELECT status FROM items WHERE id=?", (vk,)).fetchone()[0]
sstat4 = db.execute("SELECT status FROM items WHERE id=?", (vs,)).fetchone()[0]
# 核码成功会把当时的物品交还：若核码先于合并，则 vs=returned，合并随后被拒；
# 若合并先于核码，码随 claim 归到 vk，核销把 vk 置为 returned。
# 二者只能发生一种，不能两个物品都交还。
returned = [i for i, s in ((vk, kstat), (vs, sstat4)) if s == "returned"]
check("合并 vs 核码：至多一个物品交还，不产生状态矛盾",
      len(returned) <= 1, f"{rv_m} {rv_v} k={kstat} s={sstat4}")
if sstat4 == "returned":
    check("核码先提交：并入物品已交还，合并被拒",
          kstat == "kept"
          and db.execute("SELECT COUNT(*) FROM item_merges WHERE source_item_id=?",
                         (vs,)).fetchone()[0] == 0)
else:
    check("合并先提交：获选申请与码归保留物品，核销使保留物品交还",
          sstat4 == "merged" and kstat == "returned" and rv_v[0] == 303
          and db.execute("SELECT item_id FROM claims WHERE id=?", (vs_c,)).fetchone()[0] == vk)

# --------------------------------------------------------------- 场景六：访客不可见合并信息
print("== 访客可见性 ==")
_, vis = client()
st, body = req(vis, "GET", f"/staff/items/merge")
check("访客不能打开合并页(403)", st == 403)
# 未登录直接 POST
data = urllib.parse.urlencode({"csrf_token": "x", "keep_item_id": z1,
                               "source_item_id": z2, "reason": "h"}).encode()
rq = urllib.request.Request(BASE + "/staff/items/merge", data=data, method="POST")
try:
    code = urllib.request.build_opener().open(rq).status
except urllib.error.HTTPError as e:
    code = e.code
check("访客不能提交合并(403)", code == 403)
# 公开列表/旧详情均不泄露合并理由
st, body = req(vis, "GET", f"/items/{s1}")
check("访客旧详情不含合并理由/原私密", "合并理由" not in body and "挂件小熊" not in body)

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
