from ib.ext.Contract import Contract
from ib.opt import ibConnection, message
from ib.ext.Order import Order
import collections
import math
import csv
from datetime import datetime
import time
import threading
import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm
import smtplib
from email.mime.text import MIMEText
from model import *
import sys

sys.setrecursionlimit(99999999)


def datecov2(date):
    date=str(date)
    return date[0:4]+date[5:7]+date[8:10]



def get_contract(ct):

    #print 'Symbol: '+str(ct.m_symbol), 'SecType: '+str(ct.m_secType), 'Exchange: '+str(ct.m_exchange), 'Currency: '+str(ct.m_currency)
    return ct.m_symbol+'_'+ct.m_currency+'('+ct.m_secType+')'


def watcher(msg):
    print 'watcher: '+str(msg)


def make_order(action, orderId, quantity, orderType):
    order = Order()
    order.m_action=action
    order.m_orderId=orderId
    order.m_tif='GTC'
    order.m_orderType=orderType
    order.m_totalQuantity=quantity
    '''
    order.m_clientId = 0
    order.m_permid = 0
    order.m_lmtPrice = 0
    order.m_auxPrice = 0
    order.m_transmit = True
    '''
    return order


class option:

    def __init__(self, contract, K, mat_date, strategy, notional, buy_sell, set_obj):
        run_time=time.strftime("%Y%m%d_%H%M%S")
        #log_dir='C:/Users/Mengfei Zhang/Desktop/fly capital/trading/option log'
        log_dir='/Users/MengfeiZhang/Desktop/tmp'
        self.client=None
        self.K=K
        self.mat_date=mat_date #yyyymmdd
        self.T=0
        self.S=0
        self.last_price=0
        self.contract=contract
        self.underlying=contract.m_symbol+'_'+contract.m_currency+'('+contract.m_secType+')'
        self.strategy=strategy
        self.strategy_dir=[]
        self.notional=notional
        self.model=None
        self.buy_sell=buy_sell
        self.f=open(log_dir+'/'+self.underlying+'_log_'+run_time+'.txt','w')
        self.manually_close=False
        self.locker=threading.Lock()
        self.now=None
        self.updated=False
        self.update_count=0
        self.restart=True
        self.set_obj=set_obj
        self.weekday=None
        # connect
        self.connect()
        # get static data
        self.int_rate=self.get_interest_rate()
        self.sabr_calib=SABRcalib(0.5, self.T)
        self.sabr_calib.calib(self.get_hist_data('5 Y'))
        self.SABRpara=self.sabr_calib.get_para()

    def connect(self):

        def conn_status_handler(msg):
            print msg.accountsList
            if msg.accountsList!=None:
                print self.underlying+' connection succeeded...'
            else:
                print self.underlying+' connection failed...'
                time.sleep(5)
                self.connect()

        self.con = ibConnection()
        self.con.registerAll(watcher)
        self.con.register(conn_status_handler,message.managedAccounts)
        self.con.connect()
        time.sleep(1)


    def get_underlying_price(self):
        self.last_price=None
        self.bid_price=None
        self.ask_price=None
        def tick_price_handler(msg):
            if msg.field==4: #last
                self.last_price=msg.price
            elif msg.field==1: #bid
                self.bid_price=msg.price
            elif msg.field==2: #ask
                self.ask_price=msg.price
        try:
            self.con.register(tick_price_handler,message.tickPrice)
            self.con.reqMktData(1, self.contract, '', '')
            time.sleep(5)
            self.con.cancelMktData(1)

            if self.ask_price!=None and self.bid_price!=None:
                print 'price: '+str((self.ask_price+self.bid_price)/2)
                return (self.ask_price+self.bid_price)/2
            else:
                print 'price: '+str(self.last_price)
                return self.last_price

        except Exception as err:
            print >>self.f, err


    def get_hist_data(self, hist_len):
        self.hist_price=[]

        def hist_data_handler(msg):
            #print msg
            self.hist_price.append(msg.close)

        self.con.register(hist_data_handler,message.historicalData)
        self.con.reqHistoricalData(1, self.contract, self.now, hist_len, '1 day', 'MIDPOINT', 1, 1)
        time.sleep(5)
        #self.con.cancelHistoricalData(1)
        return self.hist_price[0:-2]

    def get_hist_vol(self):

        hist_data=self.get_hist_data('90 D')

        ret_tmp=[]
        for i in range(1,len(hist_data)):
            ret_tmp.append(math.log(hist_data[i]/hist_data[i-1]))

        return np.std(ret_tmp)*math.sqrt(262)

    def get_atm_vol(self):
        return self.SABRpara[0]*self.get_underlying_price()**(self.SABRpara[1]-1)

    def get_intraday_vol(self):

        return self.get_atm_vol()/math.sqrt(262)

    def get_option_value(self):
        price=0
        for i in range(0,len(self.model)):
            price+=self.model[i].price(self.S, self.int_rate['ccy2'], self.int_rate['ccy1'], self.SABRpara)*self.strategy_dir[i]
        return price

    def get_option_delta(self):
        delta=0
        for i in range(0,len(self.model)):
            delta+=self.model[i].delta(self.S, self.int_rate['ccy2'], self.int_rate['ccy1'], self.SABRpara)*self.strategy_dir[i]

        return delta


    def load_data(self):
        delta_t=datetime.strptime(self.mat_date,'%Y%m%d')-datetime.strptime(datecov2(datetime.today()),'%Y%m%d')
        self.T=float(delta_t.days)/float(365)+0.000001 #prevent expiry date error
        self.now=datetime.now()
        self.weekday=datetime.today().weekday()
        self.S=self.get_underlying_price()
        self.payoff()

    def payoff(self):
        if self.strategy=='call' or self.strategy=='put':
            self.model=[SABRmodel(self.K[0], self.T, self.strategy)]
            self.strategy_dir=[1]
        elif self.strategy=='straddle':
            self.model=[SABRmodel(self.K[0], self.T, 'call'), SABRmodel(self.K[0], self.T, 'put')]
            self.strategy_dir=[1,1]
        elif self.strategy=='call_spread':
            self.model=[SABRmodel(self.K[0], self.T, 'call'), SABRmodel(self.K[1], self.T, 'call')]
            self.strategy_dir=[1,-1]
        elif self.strategy=='put_spread':
            self.model=[SABRmodel(self.K[0], self.T, 'put'), SABRmodel(self.K[1], self.T, 'put')]
            self.strategy_dir=[1,-1]

    def get_position(self):

        self.open_position=None
        def open_position_handler(msg):

            if get_contract(msg.contract)==self.underlying:
                self.open_position=msg.pos
            else:
                self.open_position=None #no open positions

        try:

            self.con.register(open_position_handler,message.position)
            self.con.reqPositions()
            time.sleep(1)

            if self.open_position!=None:
                open_position_info={}
                open_position_info['units']=abs(self.open_position)
                if self.open_position>0:
                    open_position_info['side']='buy'
                else:
                    open_position_info['side']='sell'
            else:
                open_position_info=None

            return open_position_info

        except Exception as err:
            if ('Connection' in str(err))==False and ('handlers' in str(err))==False:
                return None
            else:
                return -99999

    def get_pos_dir(self, position):
        if self.buy_sell=='buy':
            if position>=0:
                return 'BUY'
            else:
                return 'SELL'
        else:
            if position>=0:
                return 'BUY'
            else:
                return 'SELL'

    def get_trd_dir(self, position_diff):
        if position_diff>=0:
            return 'BUY'
        else:
            return 'SELL'

    def get_interest_rate(self):
        interest={}
        interest['ccy1']=0 #div
        interest['ccy2']=0 #int

        return interest

    def start(self): #start trading
        self.load_data()

        if self.T<=0:
            if self.get_position()!=None: #if there is position open
                resp_expiry=self.client.close_position(instrument=self.underlying)
                send_hotmail('Option expired('+self.underlying+')', resp_expiry, self.set_obj)

            print >> self.f, 'option has expired...'
            return None

        try:
            print 'heartbeat('+self.underlying+') '+str(datetime.now())+'...'
            if self.get_position()==None:
                if self.manually_close==False:
                    print >>self.f,'position '+'('+self.underlying+')'+' does not exist, creating new position...'
                    self.manually_close=True
                    self.restart=False
                    position=int(self.get_option_delta()*self.notional)

                    order=make_order(self.get_pos_dir(position), 1, abs(position), 'MKT')
                    try:
                        self.con.placeOrder(1, self.contract, order)
                        self.last_price=self.S #update last price
                        print >>self.f,'Order placed: ', order
                        send_hotmail('New position opened('+self.underlying+')', {'msg':order}, self.set_obj)
                    except Exception as err:
                        print err
                        print >>self.f, err
                        if ('halt' in str(err))!=True:
                            print "order not executed..."
                            self.manually_close=False

                    print >>self.f,'price'+'('+self.underlying+')'+'= '+str(self.get_underlying_price())
                    print >>self.f,'delta= '+str(self.get_option_delta())
                    print >>self.f,'T= '+str(self.T)
                    print >>self.f,'SABR parameters: '+str(self.SABRpara)
                    print >>self.f,'ATM volatility: '+str(self.get_atm_vol())
                    print >>self.f,'interest rate '+ str(self.int_rate)
                    print >>self.f,self.get_pos_dir(position)+' '+str(abs(position))+' '+self.underlying
                    print >>self.f,'current total position is: '+self.get_position()['side']+' '+str(self.get_position()['units'])+' '+self.underlying
                    print >>self.f,self.now.strftime("%Y-%m-%d %H:%M:%S")
                    print >>self.f,'------------------------------------------------------------'
                elif self.manually_close==True and self.get_position()==None: #in case fake close position
                    print 'position ('+self.underlying+') has been manually closed...'
                    return None

            elif self.get_position()['units']>self.notional:

                resp=self.client.close_position(instrument=self.underlying)
                print >>self.f, resp
                print >>self.f, 'unusual amount of position opened, position closed...'
                return None

            else:
                if self.last_price==0:
                    self.last_price=self.S
                    print self.last_price
                ret=math.log(self.S/self.last_price)

                if abs(ret)>=3*self.get_intraday_vol():
                    send_hotmail('3 Std move('+self.underlying+')', {'msg':str(ret/self.get_intraday_vol())}, self.set_obj)
                    print '3 Std move, trading halted...'
                    return None

                position=self.get_option_delta()*self.notional
                current_position=self.get_position()['units']
                current_dir=self.get_position()['side']

                if current_dir=='SELL':
                    current_position=-current_position
                if self.buy_sell=='SELL':
                    position=-position

                position_diff=int(position-current_position)
                #schedule
                if  (int(self.now.hour) in self.set_obj.get_sche())==True:
                    if self.updated==False:
                        self.updated=True
                        self.update_count+=1
                    else:
                        self.update_count+=1
                else: #past schedule, reset parameters
                    self.updated=False
                    self.update_count=0

                if (abs(ret)>self.get_intraday_vol()/self.set_obj.get_shift_scalar() and abs(ret)<3*self.get_intraday_vol()) or (self.updated==True and self.update_count==1) or self.restart==True:
                    print >>self.f,'position '+'('+self.underlying+')'+' already exists, adjusting position...'
                    if ret*position>0:
                        pnl_dir='(+'+str(abs(position_diff))+')'
                    else:
                        pnl_dir='(-'+str(abs(position_diff))+')'

                    if self.restart==True:
                        print >> self.f, 'position restarted..'
                        msg_title='Restart position'
                        if position_diff==0:
                            position_diff=1
                        self.restart=False
                        self.manually_close=True
                    elif (abs(ret)>self.get_intraday_vol()/self.set_obj.get_shift_scalar() and abs(ret)<3*self.get_intraday_vol()):
                        print >> self.f, 'price movement > 1 std'
                        msg_title='Big price move'+pnl_dir
                    elif self.updated==True:
                        print >> self.f, 'position updated in force...'
                        msg_title='Scheduled rebalance'+pnl_dir
                        if position_diff==0:
                            position_diff=1
                    else:
                        print 'unknown error...'
                        return 0

                    order=make_order(self.get_trd_dir(position_diff), 1, abs(position_diff), 'MKT')
                    print order
                    try:
                        self.con.placeOrder(1, self.contract, order)
                        self.last_price=self.S #update last price
                        print >>self.f,'Order placed: ', order
                        send_hotmail(msg_title+'('+self.underlying+')', {'msg':order}, self.set_obj)
                    except Exception as err:
                        print err
                        print >>self.f, err
                        if ('halt' in str(err))!=True:
                            print "order not executed..."
                            self.manually_close=False


                    print >>self.f,'price'+'('+self.underlying+')'+'= '+str(self.get_underlying_price())
                    print >>self.f,'delta= '+str(self.get_option_delta())
                    print >>self.f,'T= '+str(self.T)
                    print >>self.f,'SABR parameters: '+str(self.SABRpara)
                    print >>self.f,'ATM volatility: '+str(self.get_atm_vol())
                    print >>self.f,'interest rate '+ str(self.int_rate)
                    print >>self.f,self.get_trd_dir(position_diff)+' '+str(abs(position_diff))+' '+self.underlying
                    print >>self.f,'current total position is: '+self.get_position()['side']+' '+str(self.get_position()['units'])+' '+self.underlying
                    print >>self.f,self.now.strftime("%Y-%m-%d %H:%M:%S")
                    print >>self.f,'------------------------------------------------------------'

                else: #if difference is small
                    print >>self.f,'diff less than 1 std, order will not be send...'
                    print >>self.f,'current total position is: '+self.get_position()['side']+' '+str(self.get_position()['units'])+' '+self.underlying
                    print >>self.f,self.now.strftime("%Y-%m-%d %H:%M:%S")
                    print >>self.f,'------------------------------------------------------------'
        except Exception as conn_error:

            self.con.disconnect()
            print conn_error
            print self.underlying+' disconnected, try to reconnect '+str(datetime.now())+'...'
            self.connect()

        threading.Timer(self.set_obj.get_timer(), self.start).start()


def get_option_position(fileName_, set_obj):
    contracts=[]
    file = open(fileName_, 'r')
    try:
        reader = csv.reader(file)
        for row in reader:
            ccy=row[0]
            maturity=str(row[1])
            deal_type=row[2]
            notional=int(row[3])
            side=row[4]
            if ('spread' in deal_type) != True:
                strike=[float(row[5])]
                contracts.append(option(ccy, strike, maturity, deal_type, notional, side, set_obj))
            elif ('spread' in deal_type) == True:
                strike=[float(row[5]),float(row[6])]
                contracts.append(option(ccy, strike, maturity, deal_type, notional, side, set_obj))
            else:
                print 'unknown deal type...'

    finally:
        file.close()
    return contracts

def send_hotmail(subject, content, set_obj):
    msg_txt=format_email_dict(content)
    from_email={'login': set_obj.get_email_login(), 'pwd': set_obj.get_email_pwd()}
    to_email='finatos@me.com'

    msg=MIMEText(msg_txt)
    msg['Subject'] = subject
    msg['From'] = from_email['login']
    msg['To'] = to_email
    mail=smtplib.SMTP('smtp.live.com',25)
    mail.ehlo()
    mail.starttls()
    mail.login(from_email['login'], from_email['pwd'])
    mail.sendmail(from_email['login'], to_email, msg.as_string())
    mail.close()


def format_email_dict(content):
    content_tmp=''
    for item in content.keys():
        content_tmp+=str(item)+':'+str(content[item])+'\r\n'
    return content_tmp


class set:
    def __init__(self, timer, sche, shift_scalar, login_file):
        self.timer=timer
        self.sche=sche
        self.shift_scalar=shift_scalar

        file = open(login_file, 'r')
        i=1
        try:
            reader = csv.reader(file)
            for row in reader:
                if i==1:
                    self.account_id=row[0]
                elif i==2:
                    self.token=row[0]
                elif i==3:
                    self.email_login=row[0]
                elif i==4:
                    self.email_pwd=row[0]
                i+=1

        finally:
            file.close()

    def get_timer(self):
        return self.timer

    def get_sche(self):
        return self.sche

    def get_account_id(self):
        return str(self.account_id)

    def get_token(self):
        return str(self.token)

    def get_email_login(self):
        return str(self.email_login)

    def get_email_pwd(self):
        return str(self.email_pwd)

    def get_shift_scalar(self):
        return self.shift_scalar




