import threading
import sys
from ib_function import *


def main(args):

    sche=[3, 9, 15, 21]
    timer=60
    shift_scalar=1

    login_file='/Users/MengfeiZhang/Desktop/tmp/login_info.csv'
    set_obj=set(timer, sche, shift_scalar, login_file)

    ct=Contract()
    ct.m_symbol='ES'
    ct.m_secType='FUT'
    ct.m_exchange='GLOBEX'
    ct.m_currency='USD'
    ct.m_expiry='201703'

    opt_test=option(ct,[2300],'20170331','call',10,'buy',set_obj)

    #print opt_test.get_underlying_price()

    opt_test.start()


if __name__=='__main__':
    sys.exit(main(sys.argv))


