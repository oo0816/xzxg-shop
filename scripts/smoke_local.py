"""Exercise only the isolated local platform, never real payment or model APIs."""
from datetime import datetime,timezone
from pathlib import Path
import json,re,secrets,subprocess,urllib.request,urllib.error

ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:8080/api/v1'
checks=[]

def call(route,body=None,token=None,method=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    request=urllib.request.Request(BASE+route,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    with urllib.request.urlopen(request,timeout=20) as response:
        return json.load(response)

def check(name,passed):
    checks.append({'name':name,'passed':bool(passed)})
    if not passed: raise AssertionError(name)

def stock(product_id,sku_id):
    detail=call('/products/'+product_id)
    skus=call('/products/'+product_id+'/skus')['items']
    selected=next(item for item in skus if item['skuId']==sku_id)
    return detail['stockQuantity'],selected['stockQuantity']

def expire_order_in_database(order_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',order_id):
        raise ValueError('unexpected order id')
    mysql=ROOT/'.local/mysql-8.4.13-winx64/bin/mysql.exe'
    admin=ROOT/'.local/mysql-admin.ini'
    sql=f"UPDATE orders SET payment_deadline_at=DATE_SUB(NOW(), INTERVAL 1 MINUTE) WHERE order_id='{order_id}'"
    subprocess.run([str(mysql),'--defaults-extra-file='+str(admin),'xzxg_shop_local','--execute='+sql],
                   check=True,capture_output=True)

check('health',call('/health')['status']=='ok')
user_token=None
for username,password in [('admin','admin123456'),('merchant','merchant123456'),('user','user123456')]:
    login=call('/auth/login',{'username':username,'password':password})
    check('login_'+username,bool(login.get('token')))
    if username=='user': user_token=login['token']
# Settle expired orders left by any interrupted earlier run before recording
# the global product/SKU stock baseline for this run.
call('/orders',token=user_token)
products=call('/products?page_size=100')
check('products_available',bool(products.get('items')))
all_ids={item['productId'] for item in products['items']}
for page in range(2,(products['total']+products['page_size']-1)//products['page_size']+1):
    more=call(f"/products?page={page}&page_size={products['page_size']}")
    all_ids.update(item['productId'] for item in more['items'])
check('product_pagination_total_matches_unique_items',products['total']==len(all_ids))
product=products['items'][0]
skus=call('/products/'+product['productId']+'/skus')['items']
sku=next(item for item in skus if item['stockQuantity']>=2)
initial_product_stock,initial_sku_stock=stock(product['productId'],sku['skuId'])
username='smoke_'+secrets.token_hex(5)
login=call('/auth/register',{'username':username,'password':secrets.token_hex(16),'display_name':'本地链路验收'})
token=login['token']
cart=call('/cart',token=token)
check('new_user_cart_empty',not cart.get('items'))
cart=call('/cart/items',{'product_id':product['productId'],'sku_id':sku['skuId'],'quantity':1},token)
check('add_specific_sku',cart['items'][0]['skuId']==sku['skuId'])
item=cart['items'][0]
cart=call('/cart/items/'+item['cartItemId'],{'quantity':2,'selected':True},token,method='PATCH')
check('cart_quantity_update',cart['items'][0]['quantity']==2)
preview=call('/cart/discount-preview',token=token)
check('discount_preview_available',bool(preview))
orders=call('/orders:checkout',{},token)['items']
check('checkout_pending_payment',bool(orders) and all(order['status']=='pending_payment' for order in orders))
reserved_product_stock,reserved_sku_stock=stock(product['productId'],sku['skuId'])
check('checkout_reserves_product_stock',reserved_product_stock==initial_product_stock-2)
check('checkout_reserves_sku_stock',reserved_sku_stock==initial_sku_stock-2)
for order in orders:
    detail=call('/orders/'+order['order_id'],token=token)
    check('order_detail_readable',detail['order_id']==order['order_id'])
    cancelled=call('/orders/'+order['order_id']+':cancel',{'reason':'本地链路验收，无支付'},token)
    check('cancel_pending_order',cancelled['status']=='canceled')
after_cancel_product,after_cancel_sku=stock(product['productId'],sku['skuId'])
check('cancel_restores_product_stock',after_cancel_product==initial_product_stock)
check('cancel_restores_sku_stock',after_cancel_sku==initial_sku_stock)
try:
    call('/orders/'+orders[0]['order_id']+':cancel',{'reason':'重复取消'},token)
    repeated_cancel_rejected=False
except urllib.error.HTTPError as exc:
    repeated_cancel_rejected=exc.code==409
check('repeat_cancel_rejected',repeated_cancel_rejected)
repeat_product_stock,repeat_sku_stock=stock(product['productId'],sku['skuId'])
check('repeat_cancel_does_not_add_product_stock',repeat_product_stock==initial_product_stock)
check('repeat_cancel_does_not_add_sku_stock',repeat_sku_stock==initial_sku_stock)

call('/cart/items',{'product_id':product['productId'],'sku_id':sku['skuId'],'quantity':1},token)
timeout_orders=call('/orders:checkout',{},token)['items']
check('timeout_checkout_pending_payment',bool(timeout_orders) and all(order['status']=='pending_payment' for order in timeout_orders))
timeout_reserved_product,timeout_reserved_sku=stock(product['productId'],sku['skuId'])
check('timeout_checkout_reserves_product_stock',timeout_reserved_product==initial_product_stock-1)
check('timeout_checkout_reserves_sku_stock',timeout_reserved_sku==initial_sku_stock-1)
for order in timeout_orders:
    expire_order_in_database(order['order_id'])
    detail=call('/orders/'+order['order_id'],token=token)
    check('timeout_order_closed',detail['status']=='closed_timeout')
after_timeout_product,after_timeout_sku=stock(product['productId'],sku['skuId'])
check('timeout_restores_product_stock',after_timeout_product==initial_product_stock)
check('timeout_restores_sku_stock',after_timeout_sku==initial_sku_stock)
call('/orders',token=token)
repeat_timeout_product,repeat_timeout_sku=stock(product['productId'],sku['skuId'])
check('repeat_expiry_does_not_add_product_stock',repeat_timeout_product==initial_product_stock)
check('repeat_expiry_does_not_add_sku_stock',repeat_timeout_sku==initial_sku_stock)

known_issues=[]
report={'created_at':datetime.now(timezone.utc).isoformat(),'base_url':BASE,'checks':checks,'product_count':products['total'],
        'test_account':username,'model_calls':0,'payment_calls':0,'known_issues':known_issues,'note':'new isolated MySQL database; no payment endpoint called. Failed checks remain visible.'}
output=ROOT/'.local/original-smoke.json'
output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
raise SystemExit(2 if known_issues else 0)
