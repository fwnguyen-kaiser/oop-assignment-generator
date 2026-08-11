import java.util.ArrayList;
import java.util.List;

public class Order {
    private List<Product> products;
    private Payment payment;

    public Order(List<Product> products, Payment payment) {
        this.products = products;
        this.payment = payment;
    }

    public List<Product> getProducts() {
        return this.products;
    }
    public void setProducts(List<Product> products) {
        this.products = products;
    }
    public Payment getPayment() {
        return this.payment;
    }
    public void setPayment(Payment payment) {
        this.payment = payment;
    }

}