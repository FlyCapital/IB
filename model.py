import math
from scipy.stats import norm
import numpy as np
from scipy.optimize import minimize
import matplotlib.pyplot as plt

class BSmodel:

    def __init__(self, K, T, type):
        self.K=K
        self.T=T
        self.type=type

    def price(self, S, r, d, vol):
        d1=(math.log(S/self.K)+(r-d+0.5*vol*vol)*self.T)/(vol*math.sqrt(self.T))
        d2=d1-vol*math.sqrt(self.T)

        call_price=S*math.exp(-d*self.T)*norm.cdf(d1)-self.K*math.exp(-r*self.T)*norm.cdf(d2)
        put_price=call_price-S*math.exp(-d*self.T)+self.K*math.exp(-r*self.T)

        if self.type=='call':
            return call_price
        elif self.type=='put':
            return put_price

    def delta(self, S, r, d, vol):
        d1=(math.log(S/self.K)+(r-d+0.5*vol*vol)*self.T)/(vol*math.sqrt(self.T))

        call_delta=math.exp(-d*self.T)*norm.cdf(d1)
        put_delta=-math.exp(-d*self.T)*norm.cdf(-d1)

        if self.type=='call':
            return call_delta
        elif self.type=='put':
            return put_delta

    def vega(self, S, r, d, vol):
        d1=(math.log(S/self.K)+(r-d+0.5*vol*vol)*self.T)/(vol*math.sqrt(self.T))
        vega=S*math.exp(-d*self.T)*norm.pdf(d1)*math.sqrt(self.T)

        return vega


class SABRcalib:

    def __init__(self, beta, T):
        self.T=T #T for future use
        self.alpha=0
        self.beta=beta
        self.rho=0
        self.nu=0
        self.vol_atm=None
        self.garch_para=None
        self.para=None

    def calib(self, hist_price):

        ret=price2ret(hist_price)
        T=len(ret)
        hist_alpha = np.empty(T)
        d_w1=np.empty(T-1)
        d_w2=np.empty(T-1)

        #calibrate garch model
        vol_obj=garch(ret)
        vol_obj.estimation()
        self.garch_para=vol_obj.theta
        self.vol_atm=vol_obj.get_fitted_vol()

        for i in range(0,T):
            hist_alpha[i]=self.vol_atm[i]*math.pow(hist_price[i+1], 1-self.beta)

        self.alpha=hist_alpha[-1]
        ret_alpha=price2ret(hist_alpha)
        self.nu=np.std(ret_alpha)

        hist_price_tmp=hist_price[1:]
        for i in range(1,T):
            d_w1[i-1]=(hist_price_tmp[i]-hist_price_tmp[i-1])/(hist_alpha[i-1]*pow(hist_price_tmp[i-1],self.beta))
            d_w2[i-1]=(hist_alpha[i]-hist_alpha[i-1])/(hist_alpha[i-1]*self.nu)


        self.rho=np.corrcoef(d_w1, d_w2)[0, 1]

        self.para = self.alpha, self.beta, self.rho, self.nu

    def get_para(self):

        return self.para


class SABRmodel:

    def __init__(self, K, T, type):
        self.K=K
        self.T=T
        self.type=type

    def impv(self, f, para):

        alpha, beta, rho, nu=para

        if self.K!=f:
            A=alpha/((f*self.K)**((1-beta)/2)*(1+(1-beta)**2/24*(math.log(f/self.K)**2)+(1-beta)**4/1920*math.log(f/self.K)**4))
            B=1+((1-beta)**2/24*alpha**2/((f*self.K)**(1-beta))+1/4*alpha*beta*rho*nu/((f*self.K)**((1-beta)/2))+(2-3*rho**2)/24*nu**2)*self.T
            z=nu/alpha*(f*self.K)**((1-beta)/2)*math.log(f/self.K)

            X=math.log((math.sqrt(1-2*rho*z+z**2)+z-rho)/(1-rho))
            return A*z/X*B
        else:

            B=1+((1-beta)**2/24*alpha**2/(f**(2-2*beta))+1/4*alpha*beta*rho*nu/(f**(1-beta))+(2-3*rho**2)/24*nu**2)*self.T

            return alpha*f**(beta-1)*B

    def delta(self, f, r, d, para):

        alpha, beta, rho, nu=para

        sabr_vol=self.impv(f, para)

        bs_model=BSmodel(self.K,self.T,self.type)

        bs_delta=bs_model.delta(f, r, d, sabr_vol)
        bs_vega=bs_model.vega(f, r, d, sabr_vol)

        ptg=0.005
        pdf=f*(1+ptg)
        ndf=f*(1-ptg)
        ppara=alpha*(1+ptg), beta, rho, nu
        npara=alpha*(1-ptg), beta, rho, nu

        vol_pdf=self.impv(pdf, para)
        vol_ndf=self.impv(ndf, para)
        vol_pdalpha=self.impv(f, ppara)
        vol_ndalpha=self.impv(f, npara)


        dvol_df=(vol_pdf-vol_ndf)/(pdf-ndf)
        dvol_dalpha=(vol_pdalpha-vol_ndalpha)/(alpha*(1+ptg)-alpha*(1-ptg))

        return bs_delta+bs_vega*(dvol_df+dvol_dalpha*rho*nu/(f**beta))

    def price(self, f, r, d, para):

        bs_model=BSmodel(self.K,self.T,self.type)
        sabr_vol=self.impv(f, para)

        return bs_model.price(f, r, d, sabr_vol)

class garch:

    def __init__(self, data):
        self.data=data
        self.theta=None

    def logfunc(self, theta):
        c, a, b=theta
        ret=self.data
        T = len(ret)
        ret=ret-np.mean(ret)
        h = np.empty(T)
        h[0] = np.var(ret)

        logfunc=0
        for i in range(1, T):
            h[i] = c + a*ret[i-1]**2 + b*h[i-1]  # GARCH(1,1) model
            logfunc+=-0.5*math.log(h[i])-0.5*ret[i]**2/h[i]

        return -logfunc

    def estimation(self):
        x0=[0.5,0.1,0.85]
        lb=0.0001
        bnds=[(0,10), (lb,1), (lb,1)]
        result = minimize(self.logfunc, x0, method='L-BFGS-B', bounds=bnds, options={'maxiter':99999999, 'disp': False})
        self.theta=result.x

    def get_fitted_vol(self):
        ret=self.data
        c, a, b=self.theta
        T = len(ret)
        ret=ret-np.mean(ret)
        h = np.empty(T)
        vol = np.empty(T)
        h[0] = np.var(ret)
        vol[0]=math.sqrt(h[0])

        for i in range(1, T):
            h[i] = c + a*ret[i-1]**2 + b*h[i-1]
            vol[i]=math.sqrt(h[i])

        return vol*math.sqrt(262)

def price2ret(price):
    ret_tmp=[]
    for i in range(1,len(price)):
        ret_tmp.append(math.log(price[i]/price[i-1]))
    return ret_tmp



'''
# test SABR
client = Client(
    environment=PRACTICE,
    account_id='9478887',
    access_token='26e0c37f668b7db398a7420e4ab42d29-5d3ee4328f60d27b8492560e049e3986'
)


hist_resp=client.get_instrument_history(
    instrument='USD_MXN',
    candle_format="midpoint",
    granularity="D",
    count=262*5,
)


price=[]
for i in range(0,len(hist_resp['candles'])):
    price.append(hist_resp['candles'][i]['closeMid'])


sabr_calib=SABRcalib(0.5, 1)
sabr_calib.calib(price)

print sabr_calib.get_para()

sabr_model=SABRmodel(1.14, 1, 'call')

print sabr_model.impv(1.14, sabr_calib.get_para())
print sabr_model.delta(1.2, 0, 0, sabr_calib.get_para())
print sabr_model.price(1.14, 0, 0, sabr_calib.get_para())



plt.plot(sabr_calib.vol_atm)
plt.show()
'''

